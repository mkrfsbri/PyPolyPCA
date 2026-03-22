"""
Polymarket CLOB HTTP client with built-in rate-limit back-off and retry logic.

Wraps the official py-clob-client SDK and adds:
  - Async execution via run_in_executor
  - Exponential back-off on 429 / network errors
  - Gamma API calls for market discovery
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

import config as cfg
from src.utils.logger import setup_logger

logger = logging.getLogger("polybot.client")


class RateLimiter:
    """Token-bucket rate limiter for HTTP calls."""

    def __init__(self, calls: int, window: float) -> None:
        self._calls = calls
        self._window = window
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._timestamps = [t for t in self._timestamps if now - t < self._window]
            if len(self._timestamps) >= self._calls:
                sleep_for = self._window - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            self._timestamps.append(time.monotonic())


async def _retry(coro_factory, attempts: int = cfg.API_RETRY_ATTEMPTS,
                 base_delay: float = cfg.API_RETRY_BASE_DELAY_S) -> Any:
    """Run an async factory with exponential back-off."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError) as exc:
            last_exc = exc
            delay = base_delay * (2 ** attempt)
            logger.warning("Attempt %d/%d failed (%s). Retrying in %.1fs",
                           attempt + 1, attempts, exc, delay)
            await asyncio.sleep(delay)
        except Exception as exc:
            # Non-retriable
            raise exc from None
    raise last_exc


class PolymarketClient:
    """
    Async-friendly Polymarket CLOB client.

    All blocking SDK calls are offloaded to a thread-pool executor so they
    never block the event loop.
    """

    def __init__(self) -> None:
        self._rate_limiter = RateLimiter(
            cfg.API_RATE_LIMIT_CALLS, cfg.API_RATE_LIMIT_WINDOW_S
        )
        self._http: Optional[aiohttp.ClientSession] = None
        self._clob = None       # py_clob_client.ClobClient instance
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "PyPolyPCA/1.0"},
        )
        await self._init_clob_client()
        logger.info("PolymarketClient connected")

    async def _init_clob_client(self) -> None:
        """Initialise the blocking py-clob-client in an executor."""
        def _build() -> Any:
            # Import here so missing dependency doesn't crash at module load
            from py_clob_client.client import ClobClient  # type: ignore
            from py_clob_client.clob_types import ApiCreds  # type: ignore

            creds = None
            if cfg.API_KEY and cfg.API_SECRET and cfg.API_PASSPHRASE:
                creds = ApiCreds(
                    api_key=cfg.API_KEY,
                    api_secret=cfg.API_SECRET,
                    api_passphrase=cfg.API_PASSPHRASE,
                )
            client = ClobClient(
                host=cfg.CLOB_HTTP_URL,
                chain_id=137,           # Polygon
                key=cfg.PRIVATE_KEY,
                creds=creds,
                funder=cfg.FUNDER_ADDRESS or None,
            )
            return client

        self._clob = await self._loop.run_in_executor(None, _build)

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    # ------------------------------------------------------------------
    # Market discovery via Gamma API
    # ------------------------------------------------------------------

    async def get_market_by_slug(self, slug: str) -> Optional[Dict]:
        """Fetch market details from Gamma API by slug."""
        await self._rate_limiter.acquire()
        url = f"{cfg.GAMMA_API_URL}/markets"
        params = {"slug": slug, "limit": 1}

        async def _fetch():
            async with self._http.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                return None

        return await _retry(_fetch)

    async def get_market_orderbook(self, token_id: str) -> Dict:
        """Get current orderbook snapshot for a token."""
        await self._rate_limiter.acquire()

        def _get_ob():
            return self._clob.get_order_book(token_id)

        return await _retry(
            lambda: self._loop.run_in_executor(None, _get_ob)
        )

    async def get_usdc_balance(self) -> float:
        """Return current USDC balance for the funder/proxy wallet."""
        await self._rate_limiter.acquire()

        def _bal():
            return self._clob.get_balance()

        result = await _retry(
            lambda: self._loop.run_in_executor(None, _bal)
        )
        # result is a string like "1234.567890"
        try:
            return float(result)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        side: str,          # "BUY" | "SELL"
        price: float,
        size: int,
        order_type: str = "IOC",   # "IOC" | "LIMIT" | "GTC"
    ) -> Dict:
        """Place an order through the CLOB SDK."""
        await self._rate_limiter.acquire()

        def _place():
            from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
            from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

            ot_map = {
                "IOC": OrderType.IOC,
                "LIMIT": OrderType.GTC,
                "GTC": OrderType.GTC,
                "FOK": OrderType.FOK,
            }
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=float(size),
                side=BUY if side == "BUY" else SELL,
                fee_rate_bps=0,
            )
            signed = self._clob.create_order(args)
            return self._clob.post_order(signed, ot_map.get(order_type, OrderType.GTC))

        return await _retry(
            lambda: self._loop.run_in_executor(None, _place)
        )

    async def get_order(self, order_id: str) -> Dict:
        """Fetch a single order's current state."""
        await self._rate_limiter.acquire()

        def _get():
            return self._clob.get_order(order_id)

        return await _retry(
            lambda: self._loop.run_in_executor(None, _get)
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        await self._rate_limiter.acquire()

        def _cancel():
            return self._clob.cancel(order_id)

        try:
            result = await _retry(
                lambda: self._loop.run_in_executor(None, _cancel)
            )
            return bool(result)
        except Exception as exc:
            logger.error("Cancel order %s failed: %s", order_id, exc)
            return False

    # ------------------------------------------------------------------
    # Dry-run order simulation
    # ------------------------------------------------------------------

    @staticmethod
    def simulate_fill(
        orderbook: Dict,
        side: str,     # "BUY" | "SELL"
        price: float,
        size: int,
    ) -> bool:
        """
        Check if an order *would* fill given current orderbook depth.

        For a BUY: we need enough ask volume at or below `price`.
        For a SELL: we need enough bid volume at or above `price`.
        Returns True if the order would be fully filled.
        """
        try:
            if side == "BUY":
                asks = orderbook.get("asks", [])
                available = sum(
                    float(a.get("size", 0))
                    for a in asks
                    if float(a.get("price", 999)) <= price
                )
                return available >= size
            else:
                bids = orderbook.get("bids", [])
                available = sum(
                    float(b.get("size", 0))
                    for b in bids
                    if float(b.get("price", 0)) >= price
                )
                return available >= size
        except Exception:
            return False

    @staticmethod
    def best_ask(orderbook: Dict) -> Optional[float]:
        asks = orderbook.get("asks", [])
        if not asks:
            return None
        return min(float(a["price"]) for a in asks)

    @staticmethod
    def best_bid(orderbook: Dict) -> Optional[float]:
        bids = orderbook.get("bids", [])
        if not bids:
            return None
        return max(float(b["price"]) for b in bids)
