"""
Async Multi-Market Manager

Bootstraps and orchestrates:
  - Market discovery (Gamma API)
  - WebSocket subscription for all token IDs
  - Independent StrategyEngine per market
  - Periodic market resolution monitoring and cycle reset
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import config as cfg
from src.api.client import PolymarketClient
from src.api.websocket import MarketWebSocket
from src.database.db import Database
from src.strategy.engine import StrategyEngine
from src.strategy.state import MarketState
from src.utils.ntp_sync import NTPSync

logger = logging.getLogger("polybot.manager")

# Slugs to manage
_MARKET_SLUGS = {
    "BTC_5M": cfg.BTC_5M_SLUG,
    "BTC_15M": cfg.BTC_15M_SLUG,
}


class MultiMarketManager:
    """
    Top-level coordinator.  Create one instance, call `await run()`.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._client = PolymarketClient()
        self._ws = MarketWebSocket()
        self._states: Dict[str, MarketState] = {}
        self._engines: Dict[str, StrategyEngine] = {}
        self._ntp = NTPSync.get()
        self._resolution_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Properties for dashboard consumption
    # ------------------------------------------------------------------

    @property
    def states(self) -> Dict[str, MarketState]:
        return self._states

    @property
    def client(self) -> PolymarketClient:
        return self._client

    @property
    def ws(self) -> MarketWebSocket:
        return self._ws

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info("MultiMarketManager starting…")

        await self._client.connect()

        # Discover markets and register WS subscriptions
        await self._discover_markets()

        # Register WS callback before connecting
        self._ws.add_callback(self._on_ws_event)
        await self._ws.start()

        # Allow WS a moment to receive initial snapshots
        await asyncio.sleep(1.5)

        # Start strategy engines
        for market_type, state in self._states.items():
            engine = StrategyEngine(state, self._client, self._ws, self._db)
            self._engines[market_type] = engine
            await engine.start()
            logger.info("Engine started: %s", market_type)

        # Resolution monitor
        self._resolution_task = asyncio.create_task(
            self._resolution_monitor(), name="resolution-monitor"
        )
        logger.info("MultiMarketManager fully operational")

    async def stop(self) -> None:
        logger.info("MultiMarketManager stopping…")
        for engine in self._engines.values():
            await engine.stop()
        await self._ws.stop()
        if self._resolution_task and not self._resolution_task.done():
            self._resolution_task.cancel()
            try:
                await self._resolution_task
            except asyncio.CancelledError:
                pass
        await self._client.close()

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def _discover_markets(self) -> None:
        tasks = [
            self._load_market(market_type, slug)
            for market_type, slug in _MARKET_SLUGS.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for market_type, result in zip(_MARKET_SLUGS, results):
            if isinstance(result, Exception):
                logger.error("Failed to discover %s: %s", market_type, result)

    async def _load_market(self, market_type: str, slug: str) -> None:
        data = await self._client.get_market_by_slug(slug)
        if not data:
            raise RuntimeError(f"Market not found for slug: {slug}")

        # Gamma API returns tokens list; index 0=YES, 1=NO (convention)
        tokens = data.get("tokens", [data.get("clobTokenIds", [])])
        if isinstance(tokens[0], dict):
            yes_token = tokens[0].get("token_id", "")
            no_token = tokens[1].get("token_id", "") if len(tokens) > 1 else ""
        else:
            yes_token = str(tokens[0]) if tokens else ""
            no_token = str(tokens[1]) if len(tokens) > 1 else ""

        resolution_ts: Optional[int] = None
        end_date = data.get("endDate") or data.get("end_date_iso")
        if end_date:
            import datetime
            try:
                dt = datetime.datetime.fromisoformat(
                    end_date.replace("Z", "+00:00")
                )
                resolution_ts = int(dt.timestamp() * 1000)
            except Exception:
                pass

        state = MarketState(
            market_type=market_type,
            yes_token_id=yes_token,
            no_token_id=no_token,
            resolution_ts=resolution_ts,
        )
        self._states[market_type] = state

        # Subscribe WebSocket
        if yes_token:
            self._ws.subscribe(yes_token)
        if no_token:
            self._ws.subscribe(no_token)

        logger.info(
            "Market loaded: %s | YES=%s… | NO=%s… | resolves@%s",
            market_type,
            yes_token[:10] if yes_token else "N/A",
            no_token[:10] if no_token else "N/A",
            resolution_ts,
        )

    # ------------------------------------------------------------------
    # WebSocket event handler
    # ------------------------------------------------------------------

    async def _on_ws_event(self, asset_id: str, event: dict) -> None:
        """Log every WS event; engines read state via ws.get_orderbook()."""
        etype = event.get("event_type", "unknown")
        logger.debug("WS [%s] %s", asset_id[:10], etype)

    # ------------------------------------------------------------------
    # Resolution monitor
    # ------------------------------------------------------------------

    async def _resolution_monitor(self) -> None:
        """
        Polls every second; when a market resolves, resets position state
        and re-discovers the next market cycle.
        """
        while True:
            try:
                await asyncio.sleep(1.0)
                now_ms = int(self._ntp.now() * 1000)
                for market_type, state in self._states.items():
                    if state.resolution_ts and now_ms >= state.resolution_ts:
                        logger.info(
                            "[%s] Market resolved. Resetting cycle and re-discovering…",
                            market_type
                        )
                        state.reset_cycle()
                        # Re-discover next market window
                        slug = _MARKET_SLUGS[market_type]
                        try:
                            await self._load_market(market_type, slug)
                        except Exception as exc:
                            logger.error(
                                "Re-discover failed for %s: %s", market_type, exc
                            )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Resolution monitor error: %s", exc)
