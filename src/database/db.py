"""
Async database layer – supports SQLite (aiosqlite) and PostgreSQL (asyncpg).
Schema is created on first connect; indices kept for fast strategy review queries.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

import config as cfg

logger = logging.getLogger(__name__)


class StrategyPhase(str, enum.Enum):
    INITIAL = "INITIAL"
    HEDGE = "HEDGE"
    STACKING = "STACKING"
    EMERGENCY = "EMERGENCY"


@dataclass
class Trade:
    market_type: str          # "BTC_5M" | "BTC_15M"
    pair_side: str            # "YES" | "NO"
    price: float
    shares: int
    strategy_phase: StrategyPhase
    is_dry_run: bool
    is_crash_saved: bool = False
    net_pnl: float = 0.0
    timestamp: float = field(default_factory=time.time)
    order_id: str = ""


_CREATE_SQLITE = """
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL    NOT NULL,
    market_type    TEXT    NOT NULL,
    pair_side      TEXT    NOT NULL,
    price          REAL    NOT NULL,
    shares         INTEGER NOT NULL,
    strategy_phase TEXT    NOT NULL,
    is_dry_run     INTEGER NOT NULL DEFAULT 1,
    is_crash_saved INTEGER NOT NULL DEFAULT 0,
    net_pnl        REAL    NOT NULL DEFAULT 0.0,
    order_id       TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp     ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_market        ON trades(market_type);
CREATE INDEX IF NOT EXISTS idx_trades_phase         ON trades(strategy_phase);
CREATE INDEX IF NOT EXISTS idx_trades_crash        ON trades(is_crash_saved);
CREATE INDEX IF NOT EXISTS idx_trades_dry_run      ON trades(is_dry_run);
"""

_CREATE_PG = """
CREATE TABLE IF NOT EXISTS trades (
    id             SERIAL  PRIMARY KEY,
    timestamp      DOUBLE PRECISION NOT NULL,
    market_type    TEXT    NOT NULL,
    pair_side      TEXT    NOT NULL,
    price          DOUBLE PRECISION NOT NULL,
    shares         INTEGER NOT NULL,
    strategy_phase TEXT    NOT NULL,
    is_dry_run     BOOLEAN NOT NULL DEFAULT TRUE,
    is_crash_saved BOOLEAN NOT NULL DEFAULT FALSE,
    net_pnl        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    order_id       TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp     ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_market        ON trades(market_type);
CREATE INDEX IF NOT EXISTS idx_trades_phase         ON trades(strategy_phase);
CREATE INDEX IF NOT EXISTS idx_trades_crash        ON trades(is_crash_saved);
CREATE INDEX IF NOT EXISTS idx_trades_dry_run      ON trades(is_dry_run);
"""


class Database:
    """
    Unified async database facade.
    Usage:
        db = Database()
        await db.connect()
        await db.insert_trade(trade)
        await db.close()
    """

    def __init__(self) -> None:
        self._backend = cfg.DB_BACKEND.lower()
        self._conn = None        # aiosqlite connection or asyncpg pool
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._backend == "sqlite":
            await self._connect_sqlite()
        elif self._backend == "postgresql":
            await self._connect_pg()
        else:
            raise ValueError(f"Unknown DB_BACKEND: {self._backend}")
        logger.info("Database connected (%s)", self._backend)

    async def _connect_sqlite(self) -> None:
        import aiosqlite  # type: ignore

        import os
        os.makedirs(os.path.dirname(cfg.SQLITE_PATH) or ".", exist_ok=True)

        self._conn = await aiosqlite.connect(cfg.SQLITE_PATH)
        self._conn.row_factory = aiosqlite.Row
        # Enable WAL for concurrent reads during dashboard queries
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        for stmt in _CREATE_SQLITE.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)
        await self._conn.commit()

    async def _connect_pg(self) -> None:
        import asyncpg  # type: ignore

        self._conn = await asyncpg.create_pool(
            cfg.POSTGRES_DSN, min_size=2, max_size=10
        )
        async with self._conn.acquire() as con:
            await con.execute(_CREATE_PG)

    async def close(self) -> None:
        if self._conn is None:
            return
        try:
            if self._backend == "sqlite":
                await self._conn.close()
            else:
                await self._conn.close()
        except Exception as exc:
            logger.warning("DB close error: %s", exc)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def insert_trade(self, trade: Trade) -> int:
        """Insert a trade record and return its row id."""
        async with self._lock:
            if self._backend == "sqlite":
                return await self._insert_sqlite(trade)
            return await self._insert_pg(trade)

    async def _insert_sqlite(self, t: Trade) -> int:
        sql = """
            INSERT INTO trades
                (timestamp, market_type, pair_side, price, shares,
                 strategy_phase, is_dry_run, is_crash_saved, net_pnl, order_id)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """
        async with self._conn.execute(
            sql,
            (
                t.timestamp, t.market_type, t.pair_side, t.price, t.shares,
                t.strategy_phase.value, int(t.is_dry_run), int(t.is_crash_saved),
                t.net_pnl, t.order_id,
            ),
        ) as cur:
            row_id = cur.lastrowid
        await self._conn.commit()
        return row_id

    async def _insert_pg(self, t: Trade) -> int:
        sql = """
            INSERT INTO trades
                (timestamp, market_type, pair_side, price, shares,
                 strategy_phase, is_dry_run, is_crash_saved, net_pnl, order_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING id
        """
        async with self._conn.acquire() as con:
            row = await con.fetchrow(
                sql,
                t.timestamp, t.market_type, t.pair_side, t.price, t.shares,
                t.strategy_phase.value, t.is_dry_run, t.is_crash_saved,
                t.net_pnl, t.order_id,
            )
        return row["id"]

    async def update_pnl(self, trade_id: int, net_pnl: float) -> None:
        """Update the net_pnl for a completed trade."""
        async with self._lock:
            if self._backend == "sqlite":
                await self._conn.execute(
                    "UPDATE trades SET net_pnl=? WHERE id=?", (net_pnl, trade_id)
                )
                await self._conn.commit()
            else:
                async with self._conn.acquire() as con:
                    await con.execute(
                        "UPDATE trades SET net_pnl=$1 WHERE id=$2", net_pnl, trade_id
                    )

    # ------------------------------------------------------------------
    # Read helpers (used by dashboard)
    # ------------------------------------------------------------------

    async def get_emergency_saves_count(self) -> int:
        if self._backend == "sqlite":
            async with self._conn.execute(
                "SELECT COUNT(*) FROM trades WHERE is_crash_saved=1"
            ) as cur:
                row = await cur.fetchone()
            return row[0] if row else 0
        else:
            async with self._conn.acquire() as con:
                return await con.fetchval(
                    "SELECT COUNT(*) FROM trades WHERE is_crash_saved=TRUE"
                )

    async def get_total_pnl(self, market_type: Optional[str] = None) -> float:
        if self._backend == "sqlite":
            if market_type:
                async with self._conn.execute(
                    "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE market_type=?",
                    (market_type,),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with self._conn.execute(
                    "SELECT COALESCE(SUM(net_pnl),0) FROM trades"
                ) as cur:
                    row = await cur.fetchone()
            return float(row[0]) if row else 0.0
        else:
            async with self._conn.acquire() as con:
                if market_type:
                    return await con.fetchval(
                        "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE market_type=$1",
                        market_type,
                    ) or 0.0
                return await con.fetchval(
                    "SELECT COALESCE(SUM(net_pnl),0) FROM trades"
                ) or 0.0

    async def get_recent_trades(self, limit: int = 20) -> List[dict]:
        if self._backend == "sqlite":
            async with self._conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        else:
            async with self._conn.acquire() as con:
                rows = await con.fetch(
                    "SELECT * FROM trades ORDER BY timestamp DESC LIMIT $1", limit
                )
            return [dict(r) for r in rows]
