"""
NTP time synchronization.

Keeps a running `offset_s` (seconds) so that:
    adjusted_time = time.time() + offset_s

The offset is updated every NTP_SYNC_INTERVAL_S seconds in the background.
All callers should use `NTPSync.now()` instead of `time.time()` to get
clock-aligned timestamps.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import ntplib  # type: ignore

import config as cfg

logger = logging.getLogger("polybot.ntp")


class NTPSync:
    """Singleton-style NTP clock.  Call `await NTPSync.start()` once at boot."""

    _instance: Optional["NTPSync"] = None

    def __init__(self) -> None:
        self.offset_s: float = 0.0
        self._task: Optional[asyncio.Task] = None
        self._client = ntplib.NTPClient()

    # ------------------------------------------------------------------
    # Class-level helpers so callers do not need to hold a reference
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> "NTPSync":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def now(cls) -> float:
        """Return NTP-adjusted Unix timestamp (seconds)."""
        inst = cls.get()
        return time.time() + inst.offset_s

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Perform initial sync then schedule background refreshes."""
        await self._sync_once()
        self._task = asyncio.create_task(self._background_loop(), name="ntp-sync")
        logger.info("NTP sync started (offset=%.3fs)", self.offset_s)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _sync_once(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None, self._client.request, cfg.NTP_HOST, 3
            )
            self.offset_s = response.offset
            logger.debug("NTP offset updated: %.6f s", self.offset_s)
        except Exception as exc:
            logger.warning("NTP sync failed: %s – using system clock", exc)

    async def _background_loop(self) -> None:
        while True:
            await asyncio.sleep(cfg.NTP_SYNC_INTERVAL_S)
            await self._sync_once()

    # ------------------------------------------------------------------
    # Convenience: seconds until a future epoch-ms timestamp
    # ------------------------------------------------------------------

    def seconds_until(self, epoch_ms: int) -> float:
        return (epoch_ms / 1000.0) - self.now()
