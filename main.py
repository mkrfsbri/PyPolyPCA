"""
PyPolyPCA – Polymarket HFT Bot
Entry point.  Run with:
    python main.py

Environment variables (or set in .env):
    POLY_PRIVATE_KEY      – EOA private key (hex, no 0x prefix)
    POLY_FUNDER_ADDRESS   – Proxy wallet funder address (optional)
    POLY_API_KEY          – CLOB API key
    POLY_API_SECRET       – CLOB API secret
    POLY_API_PASSPHRASE   – CLOB API passphrase
    DB_BACKEND            – "sqlite" (default) or "postgresql"
    SQLITE_PATH           – path to SQLite file (default: data/trades.db)
    POSTGRES_DSN          – full PostgreSQL DSN (if DB_BACKEND=postgresql)
    LOG_LEVEL             – DEBUG / INFO / WARNING (default: INFO)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

# Load .env if python-dotenv is installed
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass

import config as cfg
from src.database.db import Database
from src.monitoring.dashboard import Dashboard
from src.strategy.market_manager import MultiMarketManager
from src.utils.logger import setup_logger
from src.utils.ntp_sync import NTPSync

logger = setup_logger("polybot")


async def main() -> None:
    # ── Startup banner ──────────────────────────────────────────────
    mode = "DRY RUN (paper trading)" if cfg.DRY_RUN else "LIVE TRADING"
    logger.info("=" * 60)
    logger.info("PyPolyPCA starting – Mode: %s", mode)
    logger.info("=" * 60)

    if cfg.DRY_RUN:
        logger.warning(
            "DRY_RUN=True: No real orders will be placed. "
            "Set DRY_RUN=False in config.py for live trading."
        )

    if not cfg.DRY_RUN and not cfg.PRIVATE_KEY:
        logger.error(
            "POLY_PRIVATE_KEY is not set but DRY_RUN=False. "
            "Cannot proceed with live trading."
        )
        sys.exit(1)

    # ── NTP sync ────────────────────────────────────────────────────
    ntp = NTPSync.get()
    await ntp.start()

    # ── Database ────────────────────────────────────────────────────
    db = Database()
    await db.connect()

    # ── Market manager ──────────────────────────────────────────────
    manager = MultiMarketManager(db=db)
    await manager.start()

    # ── Dashboard ───────────────────────────────────────────────────
    dashboard = Dashboard(
        states=manager.states,
        client=manager.client,
        ws=manager.ws,
        db=db,
    )
    await dashboard.start()

    # ── Graceful shutdown via SIGINT / SIGTERM ───────────────────────
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows does not support add_signal_handler
            signal.signal(sig, lambda s, f: shutdown_event.set())

    logger.info("Bot running. Press Ctrl-C to stop.")
    await shutdown_event.wait()

    # ── Teardown ────────────────────────────────────────────────────
    logger.info("Shutting down…")
    await dashboard.stop()
    await manager.stop()
    await ntp.stop()
    await db.close()
    logger.info("PyPolyPCA stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
