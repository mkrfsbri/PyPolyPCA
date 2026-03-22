"""
Polymarket CLOB WebSocket subscriber.

Subscribes to the "market" channel for real-time price and orderbook updates.
Uses an exponential back-off reconnection loop so connectivity interruptions
do not crash the bot.

Emits parsed snapshots to a registered callback:
    async def on_update(market_id: str, event: dict) -> None
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets
from websockets.exceptions import (
    ConnectionClosedError,
    ConnectionClosedOK,
    WebSocketException,
)

import config as cfg

logger = logging.getLogger("polybot.ws")

# Callback type: receives the asset_id (token_id) and parsed event dict
UpdateCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class MarketWebSocket:
    """
    Manages a single WebSocket connection to the Polymarket CLOB channel.

    Multiple token IDs can be subscribed on a single connection.
    """

    def __init__(self) -> None:
        self._subscriptions: List[str] = []     # token IDs
        self._callbacks: List[UpdateCallback] = []
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._orderbooks: Dict[str, Dict] = {}   # latest OB per token
        self._prices: Dict[str, float] = {}       # latest mid-price per token

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, token_id: str) -> None:
        if token_id not in self._subscriptions:
            self._subscriptions.append(token_id)

    def add_callback(self, cb: UpdateCallback) -> None:
        self._callbacks.append(cb)

    def get_orderbook(self, token_id: str) -> Dict:
        return self._orderbooks.get(token_id, {"bids": [], "asks": []})

    def get_price(self, token_id: str) -> Optional[float]:
        return self._prices.get(token_id)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connection_loop(), name="ws-loop")
        logger.info("WebSocket manager started for %d tokens", len(self._subscriptions))

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Connection loop with reconnect
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._run_connection()
                attempt = 0   # successful connection; reset backoff
            except asyncio.CancelledError:
                break
            except (ConnectionClosedError, WebSocketException, OSError) as exc:
                attempt += 1
                delay = min(
                    cfg.WS_RECONNECT_DELAY_S * (2 ** (attempt - 1)), 60.0
                )
                if attempt <= cfg.WS_MAX_RECONNECT_ATTEMPTS:
                    logger.warning(
                        "WS disconnected (%s). Reconnect #%d in %.1fs",
                        exc, attempt, delay
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("WS max reconnects reached. Giving up.")
                    self._running = False
                    break
            except Exception as exc:
                logger.exception("Unexpected WS error: %s", exc)
                await asyncio.sleep(cfg.WS_RECONNECT_DELAY_S)

    async def _run_connection(self) -> None:
        url = cfg.CLOB_WS_URL + "market"
        logger.info("Connecting to WebSocket: %s", url)

        async with websockets.connect(
            url,
            ping_interval=cfg.WS_PING_INTERVAL_S,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            await self._send_subscription(ws)
            logger.info("WS subscribed to %d assets", len(self._subscriptions))

            async for raw_msg in ws:
                if not self._running:
                    break
                await self._handle_message(raw_msg)

    async def _send_subscription(self, ws) -> None:
        sub_msg = {
            "auth": {},
            "markets": [],
            "assets_ids": self._subscriptions,
            "type": "market",
        }
        await ws.send(json.dumps(sub_msg))

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    async def _handle_message(self, raw: str) -> None:
        try:
            events = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Non-JSON WS message: %s", raw[:200])
            return

        if not isinstance(events, list):
            events = [events]

        for event in events:
            asset_id = event.get("asset_id", "")
            etype = event.get("event_type", "")

            if etype == "book":
                self._update_orderbook(asset_id, event)
            elif etype in ("price_change", "last_trade_price"):
                self._update_price(asset_id, event)
            elif etype == "tick_size_change":
                pass  # informational only

            # Dispatch to all registered callbacks
            if asset_id:
                for cb in self._callbacks:
                    try:
                        await cb(asset_id, event)
                    except Exception as exc:
                        logger.error("Callback error for %s: %s", asset_id, exc)

    def _update_orderbook(self, asset_id: str, event: dict) -> None:
        ob = self._orderbooks.setdefault(asset_id, {"bids": [], "asks": []})

        # Full snapshot
        if event.get("market"):
            ob["bids"] = event.get("bids", [])
            ob["asks"] = event.get("asks", [])
            return

        # Delta update
        for side_key, side_list_key in (("bid", "bids"), ("ask", "asks")):
            changes = event.get(side_key, [])
            if not changes:
                continue
            ob_side: list = ob[side_list_key]
            price_map = {item["price"]: item for item in ob_side}
            for change in changes:
                p = change["price"]
                s = float(change.get("size", 0))
                if s == 0:
                    price_map.pop(p, None)
                else:
                    price_map[p] = {"price": p, "size": s}
            ob[side_list_key] = list(price_map.values())

        # Recompute mid-price
        self._update_price_from_ob(asset_id)

    def _update_price(self, asset_id: str, event: dict) -> None:
        price = event.get("price") or event.get("last_trade_price")
        if price is not None:
            try:
                self._prices[asset_id] = float(price)
            except (ValueError, TypeError):
                pass

    def _update_price_from_ob(self, asset_id: str) -> None:
        ob = self._orderbooks.get(asset_id, {})
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if bids and asks:
            best_bid = max(float(b["price"]) for b in bids)
            best_ask = min(float(a["price"]) for a in asks)
            self._prices[asset_id] = (best_bid + best_ask) / 2.0
