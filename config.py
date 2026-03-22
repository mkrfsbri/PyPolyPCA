"""
Polymarket HFT Bot - Central Configuration
All tunable parameters and feature flags live here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
DRY_RUN: bool = True           # True → paper-trade only, no real orders sent

# ---------------------------------------------------------------------------
# Wallet / Auth
# ---------------------------------------------------------------------------
PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER_ADDRESS: str = os.getenv("POLY_FUNDER_ADDRESS", "")  # Proxy-wallet funder
API_KEY: str = os.getenv("POLY_API_KEY", "")
API_SECRET: str = os.getenv("POLY_API_SECRET", "")
API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")

# ---------------------------------------------------------------------------
# Polymarket endpoints
# ---------------------------------------------------------------------------
CLOB_HTTP_URL: str = "https://clob.polymarket.com"
CLOB_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"
GAMMA_API_URL: str = "https://gamma-api.polymarket.com"

# ---------------------------------------------------------------------------
# Market identifiers   (condition IDs resolved at run-time via Gamma API)
# ---------------------------------------------------------------------------
BTC_5M_SLUG: str = "btc-price-5m"
BTC_15M_SLUG: str = "btc-price-15m"

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
ORDER_SHARES: int = 10            # Fixed share size for every order

ENTRY_BUY_THRESHOLD: float = 0.35      # Buy if best ask < this
HEDGE_BUY_THRESHOLD: float = 0.97     # Hedge if opp side < this

STACK_PRICE_LOW: float = 0.85         # Stacking trigger lower bound
STACK_PRICE_HIGH: float = 0.95        # Stacking trigger upper bound
STACK_TRIGGER_SECONDS: int = 15       # Seconds before resolution

CRASH_PRICE_THRESHOLD: float = 0.25   # Flash-crash trigger
CRASH_TARGET_SHARES: int = 20         # Equilibrium target per side

# ---------------------------------------------------------------------------
# Order behaviour
# ---------------------------------------------------------------------------
ORDER_TYPE_ENTRY: str = "IOC"    # Immediate-or-Cancel for initial entries
ORDER_TYPE_STACK: str = "LIMIT"
ORDER_TYPE_CRASH: str = "LIMIT"
ORDER_SLIPPAGE_BPS: int = 10     # Max slippage in basis points

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_BACKEND: str = os.getenv("DB_BACKEND", "sqlite")   # "sqlite" | "postgresql"
SQLITE_PATH: str = os.getenv("SQLITE_PATH", "data/trades.db")
POSTGRES_DSN: str = os.getenv(
    "POSTGRES_DSN", "postgresql://user:pass@localhost/polymarket"
)

# ---------------------------------------------------------------------------
# NTP
# ---------------------------------------------------------------------------
NTP_HOST: str = "pool.ntp.org"
NTP_SYNC_INTERVAL_S: int = 300   # Re-sync every 5 min

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR: str = "logs"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_ROTATE_MB: int = 50

# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
WS_RECONNECT_DELAY_S: float = 2.0
WS_MAX_RECONNECT_ATTEMPTS: int = 10
WS_PING_INTERVAL_S: float = 20.0

# ---------------------------------------------------------------------------
# Rate-limiting / back-off
# ---------------------------------------------------------------------------
API_RATE_LIMIT_CALLS: int = 10       # calls per window
API_RATE_LIMIT_WINDOW_S: float = 1.0
API_RETRY_ATTEMPTS: int = 5
API_RETRY_BASE_DELAY_S: float = 0.5

# ---------------------------------------------------------------------------
# Dashboard refresh
# ---------------------------------------------------------------------------
DASHBOARD_REFRESH_HZ: float = 2.0   # redraws per second


@dataclass
class MarketConfig:
    slug: str
    condition_id: str = ""          # filled at runtime
    yes_token_id: str = ""
    no_token_id: str = ""
    resolution_ts: Optional[int] = None   # Unix epoch ms


BTC_5M_CONFIG = MarketConfig(slug=BTC_5M_SLUG)
BTC_15M_CONFIG = MarketConfig(slug=BTC_15M_SLUG)
