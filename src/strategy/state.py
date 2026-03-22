"""
Per-market position state, shared between StrategyEngine and the dashboard.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PositionSide:
    """Tracks shares and cost basis for one side (YES or NO)."""
    shares: int = 0
    avg_cost: float = 0.0       # USDC per share
    total_cost: float = 0.0     # cumulative USDC spent

    def add(self, shares: int, price: float) -> None:
        total = self.shares + shares
        self.total_cost += price * shares
        self.avg_cost = self.total_cost / total if total else 0.0
        self.shares = total

    def unrealized_pnl(self, current_price: float) -> float:
        return self.shares * (current_price - self.avg_cost)


@dataclass
class MarketState:
    """Full position + phase tracking for one BTC market."""
    market_type: str            # "BTC_5M" | "BTC_15M"
    yes_token_id: str = ""
    no_token_id: str = ""
    resolution_ts: Optional[int] = None   # epoch ms

    yes: PositionSide = field(default_factory=PositionSide)
    no: PositionSide = field(default_factory=PositionSide)

    stacking_active: bool = False
    emergency_active: bool = False
    emergency_save_count: int = 0

    # Open order ids currently live in the book
    open_orders: Dict[str, str] = field(default_factory=dict)   # {order_id: phase}

    # Lock to prevent concurrent strategy triggers
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Cycle counters
    cycle_start_ts: float = field(default_factory=time.time)

    def reset_cycle(self) -> None:
        """Called when a market resolves and a new cycle begins."""
        self.yes = PositionSide()
        self.no = PositionSide()
        self.stacking_active = False
        self.emergency_active = False
        self.open_orders.clear()
        self.cycle_start_ts = time.time()

    @property
    def total_yes_shares(self) -> int:
        return self.yes.shares

    @property
    def total_no_shares(self) -> int:
        return self.no.shares

    def unrealized_pnl(self, yes_price: float, no_price: float) -> float:
        return (
            self.yes.unrealized_pnl(yes_price)
            + self.no.unrealized_pnl(no_price)
        )
