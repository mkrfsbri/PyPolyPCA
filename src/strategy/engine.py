"""
Core Strategy Engine – executes all trading logic for a single market.

Phase progression:
  1. INITIAL  – Buy YES or NO if best-ask < 0.35 (IOC, 10 shares)
  2. HEDGE    – If one side held, buy opposite if ask < 0.97 (IOC, 10 shares)
  3. STACKING – At T-15s: if held pair price in [0.85, 0.95] → Limit Buy 10 shares
  4. EMERGENCY– If stacking active AND winning side drops < 0.25 → aggressive
                 Limit Buy on opposite side to reach 20 vs 20 equilibrium

DRY_RUN mode: all order paths are simulated against live orderbook depth;
              results are written to DB with is_dry_run=True.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import config as cfg
from src.api.client import PolymarketClient
from src.api.websocket import MarketWebSocket
from src.database.db import Database, StrategyPhase, Trade
from src.strategy.state import MarketState
from src.utils.ntp_sync import NTPSync

logger = logging.getLogger("polybot.engine")


class StrategyEngine:
    """Runs the HFT strategy for one market (BTC-5m or BTC-15m)."""

    def __init__(
        self,
        state: MarketState,
        client: PolymarketClient,
        ws: MarketWebSocket,
        db: Database,
    ) -> None:
        self._state = state
        self._client = client
        self._ws = ws
        self._db = db
        self._ntp = NTPSync.get()
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._strategy_loop(), name=f"engine-{self._state.market_type}"
        )
        logger.info("StrategyEngine started for %s", self._state.market_type)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _strategy_loop(self) -> None:
        while True:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(
                    "[%s] Unhandled error in strategy loop: %s",
                    self._state.market_type, exc
                )
                await asyncio.sleep(1.0)

    async def _run_cycle(self) -> None:
        """One iteration of the strategy evaluation."""
        state = self._state

        async with state._lock:
            yes_ob = self._ws.get_orderbook(state.yes_token_id)
            no_ob = self._ws.get_orderbook(state.no_token_id)

            yes_ask = self._client.best_ask(yes_ob)
            no_ask = self._client.best_ask(no_ob)
            yes_bid = self._client.best_bid(yes_ob)
            no_bid = self._client.best_bid(no_ob)

            # Derive mid-prices for P&L / stacking checks
            yes_price = self._ws.get_price(state.yes_token_id) or (yes_ask or 0.5)
            no_price = self._ws.get_price(state.no_token_id) or (no_ask or 0.5)

            secs_left = (
                self._ntp.seconds_until(state.resolution_ts)
                if state.resolution_ts
                else 9999.0
            )

            # ── Phase 1: INITIAL entry ──────────────────────────────────
            if state.yes.shares == 0 and state.no.shares == 0:
                await self._try_initial_entry(yes_ask, no_ask, yes_ob, no_ob)

            # ── Phase 2: HEDGE ─────────────────────────────────────────
            elif state.yes.shares > 0 and state.no.shares == 0 and no_ask is not None:
                if no_ask < cfg.HEDGE_BUY_THRESHOLD:
                    await self._execute_order(
                        token_id=state.no_token_id,
                        side="BUY",
                        price=no_ask,
                        orderbook=no_ob,
                        phase=StrategyPhase.HEDGE,
                        pair_side="NO",
                    )

            elif state.no.shares > 0 and state.yes.shares == 0 and yes_ask is not None:
                if yes_ask < cfg.HEDGE_BUY_THRESHOLD:
                    await self._execute_order(
                        token_id=state.yes_token_id,
                        side="BUY",
                        price=yes_ask,
                        orderbook=yes_ob,
                        phase=StrategyPhase.HEDGE,
                        pair_side="YES",
                    )

            # ── Phase 3: STACKING (T-15s) ──────────────────────────────
            if (
                not state.stacking_active
                and state.yes.shares > 0
                and state.no.shares > 0
                and 0 < secs_left <= cfg.STACK_TRIGGER_SECONDS
            ):
                await self._try_stacking(yes_price, no_price, yes_ob, no_ob)

            # ── Phase 4: EMERGENCY flash-crash ─────────────────────────
            if (
                state.stacking_active
                and not state.emergency_active
                and secs_left < cfg.STACK_TRIGGER_SECONDS
            ):
                await self._check_emergency(yes_price, no_price, yes_ob, no_ob)

        # Yield control – evaluate ~10× per second
        await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Phase helpers
    # ------------------------------------------------------------------

    async def _try_initial_entry(
        self, yes_ask, no_ask, yes_ob, no_ob
    ) -> None:
        state = self._state
        # Prefer whichever side is cheaper below threshold
        if yes_ask is not None and yes_ask < cfg.ENTRY_BUY_THRESHOLD:
            await self._execute_order(
                token_id=state.yes_token_id,
                side="BUY",
                price=yes_ask,
                orderbook=yes_ob,
                phase=StrategyPhase.INITIAL,
                pair_side="YES",
                order_type=cfg.ORDER_TYPE_ENTRY,
            )
        elif no_ask is not None and no_ask < cfg.ENTRY_BUY_THRESHOLD:
            await self._execute_order(
                token_id=state.no_token_id,
                side="BUY",
                price=no_ask,
                orderbook=no_ob,
                phase=StrategyPhase.INITIAL,
                pair_side="NO",
                order_type=cfg.ORDER_TYPE_ENTRY,
            )

    async def _try_stacking(
        self, yes_price: float, no_price: float, yes_ob: dict, no_ob: dict
    ) -> None:
        state = self._state
        # Determine which side is winning (higher price)
        winning_side = "YES" if yes_price >= no_price else "NO"
        winning_price = yes_price if winning_side == "YES" else no_price
        winning_ob = yes_ob if winning_side == "YES" else no_ob
        winning_token = state.yes_token_id if winning_side == "YES" else state.no_token_id

        if cfg.STACK_PRICE_LOW <= winning_price <= cfg.STACK_PRICE_HIGH:
            logger.info(
                "[%s] STACKING trigger: %s @ %.4f (T-%.1fs)",
                state.market_type, winning_side, winning_price,
                self._ntp.seconds_until(state.resolution_ts or 0)
            )
            await self._execute_order(
                token_id=winning_token,
                side="BUY",
                price=winning_price,
                orderbook=winning_ob,
                phase=StrategyPhase.STACKING,
                pair_side=winning_side,
                order_type=cfg.ORDER_TYPE_STACK,
            )
            state.stacking_active = True

    async def _check_emergency(
        self, yes_price: float, no_price: float, yes_ob: dict, no_ob: dict
    ) -> None:
        state = self._state
        # If stacking is active, one side has 20 shares; find which dropped
        yes_total = state.yes.shares
        no_total = state.no.shares

        if yes_total > no_total and yes_price < cfg.CRASH_PRICE_THRESHOLD:
            # YES stacked (20 shares) but now crashing; buy 10 NO to equalise
            needed = cfg.CRASH_TARGET_SHARES - no_total
            if needed > 0:
                logger.warning(
                    "[%s] EMERGENCY: YES crashed to %.4f – buying %d NO shares",
                    state.market_type, yes_price, needed
                )
                await self._execute_order(
                    token_id=state.no_token_id,
                    side="BUY",
                    price=self._client.best_ask(no_ob) or cfg.CRASH_PRICE_THRESHOLD,
                    orderbook=no_ob,
                    phase=StrategyPhase.EMERGENCY,
                    pair_side="NO",
                    shares_override=needed,
                    is_crash_saved=True,
                )
                state.emergency_active = True
                state.emergency_save_count += 1

        elif no_total > yes_total and no_price < cfg.CRASH_PRICE_THRESHOLD:
            # NO stacked (20 shares) but now crashing; buy 10 YES to equalise
            needed = cfg.CRASH_TARGET_SHARES - yes_total
            if needed > 0:
                logger.warning(
                    "[%s] EMERGENCY: NO crashed to %.4f – buying %d YES shares",
                    state.market_type, no_price, needed
                )
                await self._execute_order(
                    token_id=state.yes_token_id,
                    side="BUY",
                    price=self._client.best_ask(yes_ob) or cfg.CRASH_PRICE_THRESHOLD,
                    orderbook=yes_ob,
                    phase=StrategyPhase.EMERGENCY,
                    pair_side="YES",
                    shares_override=needed,
                    is_crash_saved=True,
                )
                state.emergency_active = True
                state.emergency_save_count += 1

    # ------------------------------------------------------------------
    # Order execution (live or dry-run)
    # ------------------------------------------------------------------

    async def _execute_order(
        self,
        token_id: str,
        side: str,
        price: float,
        orderbook: dict,
        phase: StrategyPhase,
        pair_side: str,
        order_type: str = cfg.ORDER_TYPE_ENTRY,
        shares_override: Optional[int] = None,
        is_crash_saved: bool = False,
    ) -> None:
        state = self._state
        shares = shares_override if shares_override is not None else cfg.ORDER_SHARES

        trade = Trade(
            market_type=state.market_type,
            pair_side=pair_side,
            price=price,
            shares=shares,
            strategy_phase=phase,
            is_dry_run=cfg.DRY_RUN,
            is_crash_saved=is_crash_saved,
        )

        if cfg.DRY_RUN:
            filled = self._client.simulate_fill(orderbook, side, price, shares)
            log_msg = "DRY-RUN %s %s %d shares @ %.4f [%s] – %s"
            logger.info(
                log_msg, phase.value, pair_side, shares, price,
                state.market_type, "FILLED" if filled else "REJECTED (no liquidity)"
            )
            if filled:
                self._update_position(state, pair_side, price, shares)
                await self._db.insert_trade(trade)
        else:
            try:
                result = await self._client.place_order(
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=shares,
                    order_type=order_type,
                )
                order_id = result.get("orderID", "")
                trade.order_id = order_id
                logger.info(
                    "LIVE %s %s %d shares @ %.4f [%s] → order_id=%s",
                    phase.value, pair_side, shares, price,
                    state.market_type, order_id[:8] if order_id else "N/A"
                )
                self._update_position(state, pair_side, price, shares)
                await self._db.insert_trade(trade)
            except Exception as exc:
                logger.error(
                    "Order failed [%s] %s %s: %s",
                    state.market_type, phase.value, pair_side, exc
                )

    @staticmethod
    def _update_position(state: MarketState, pair_side: str, price: float, shares: int) -> None:
        if pair_side == "YES":
            state.yes.add(shares, price)
        else:
            state.no.add(shares, price)
