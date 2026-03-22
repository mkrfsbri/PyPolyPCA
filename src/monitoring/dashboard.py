"""
Real-time CLI dashboard using the `rich` library.

Displays (refreshed DASHBOARD_REFRESH_HZ times/second):
  ┌───────────────────────────────────────────────────────────────┐
  │  PyPolyPCA – Polymarket HFT Bot         [DRY RUN / LIVE]     │
  ├────────────────┬──────────────────────────────────────────────┤
  │  USDC Balance  │  $1,234.56                                   │
  │  Emergency     │  Saves: 3                                    │
  ├────────────────┴──────────────────────────────────────────────┤
  │  BTC-5M                                                        │
  │   YES  10 shares @ 0.3200  |  NO   10 shares @ 0.3100        │
  │   Unrealized PnL: +$0.30                                       │
  │   Phase: HEDGE | T-minus: 42.3s                               │
  ├───────────────────────────────────────────────────────────────┤
  │  BTC-15M                                                       │
  │   YES   0 shares           |  NO   10 shares @ 0.2800        │
  │   Unrealized PnL: -$0.20                                       │
  │   Phase: INITIAL | T-minus: 823.7s                            │
  ├───────────────────────────────────────────────────────────────┤
  │  Recent Trades (last 10)                                       │
  │   ts | market | side | price | shares | phase | pnl          │
  └───────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Dict, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config as cfg
from src.utils.ntp_sync import NTPSync

if TYPE_CHECKING:
    from src.api.client import PolymarketClient
    from src.api.websocket import MarketWebSocket
    from src.database.db import Database
    from src.strategy.state import MarketState

logger = logging.getLogger("polybot.dashboard")


class Dashboard:
    def __init__(
        self,
        states: Dict[str, "MarketState"],
        client: "PolymarketClient",
        ws: "MarketWebSocket",
        db: "Database",
    ) -> None:
        self._states = states
        self._client = client
        self._ws = ws
        self._db = db
        self._ntp = NTPSync.get()
        self._console = Console()
        self._task: Optional[asyncio.Task] = None
        self._balance: float = 0.0
        self._emergency_count: int = 0
        self._recent_trades: list = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name="dashboard")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Render loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        refresh_s = 1.0 / cfg.DASHBOARD_REFRESH_HZ
        with Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=cfg.DASHBOARD_REFRESH_HZ,
            screen=True,
        ) as live:
            while True:
                try:
                    await self._refresh_data()
                    live.update(self._build_layout())
                    await asyncio.sleep(refresh_s)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.debug("Dashboard render error: %s", exc)
                    await asyncio.sleep(refresh_s)

    async def _refresh_data(self) -> None:
        """Pull fresh data from DB and client (non-blocking)."""
        try:
            self._balance = await self._client.get_usdc_balance()
        except Exception:
            pass
        try:
            self._emergency_count = await self._db.get_emergency_saves_count()
        except Exception:
            pass
        try:
            self._recent_trades = await self._db.get_recent_trades(limit=10)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Layout builders
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._header_panel(), name="header", size=3),
            Layout(self._summary_panel(), name="summary", size=5),
            Layout(self._markets_panel(), name="markets", minimum_size=10),
            Layout(self._trades_panel(), name="trades", minimum_size=8),
        )
        return layout

    def _header_panel(self) -> Panel:
        mode = "[bold red]LIVE TRADING[/]" if not cfg.DRY_RUN else "[bold yellow]DRY RUN[/]"
        title = Text.from_markup(
            f"[bold cyan]PyPolyPCA – Polymarket HFT Bot[/]   {mode}",
        )
        return Panel(title, style="bold white on navy_blue")

    def _summary_panel(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(style="bold white")

        table.add_row("USDC Balance:", f"${self._balance:,.4f}")
        table.add_row("Emergency Saves:", str(self._emergency_count))
        table.add_row("NTP Offset:", f"{self._ntp.offset_s:+.3f}s")

        return Panel(table, title="[bold]Account Summary[/]", border_style="cyan")

    def _markets_panel(self) -> Panel:
        content = Layout()
        market_panels = []

        for market_type, state in self._states.items():
            market_panels.append(
                Layout(self._single_market_panel(state), name=market_type)
            )

        if market_panels:
            content.split_row(*market_panels)

        return Panel(content, title="[bold]Markets[/]", border_style="green")

    def _single_market_panel(self, state: "MarketState") -> Panel:
        yes_price = self._ws.get_price(state.yes_token_id) or 0.0
        no_price = self._ws.get_price(state.no_token_id) or 0.0
        upnl = state.unrealized_pnl(yes_price, no_price)
        upnl_color = "green" if upnl >= 0 else "red"

        secs_left = (
            self._ntp.seconds_until(state.resolution_ts)
            if state.resolution_ts
            else None
        )
        t_str = f"{secs_left:.1f}s" if secs_left is not None else "N/A"

        # Determine current phase label
        if state.emergency_active:
            phase_label = "[bold red]EMERGENCY[/]"
        elif state.stacking_active:
            phase_label = "[bold yellow]STACKING[/]"
        elif state.yes.shares > 0 and state.no.shares > 0:
            phase_label = "[green]HEDGED[/]"
        elif state.yes.shares > 0 or state.no.shares > 0:
            phase_label = "[yellow]HEDGE PENDING[/]"
        else:
            phase_label = "[dim]WATCHING[/]"

        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True)
        table.add_column()

        table.add_row(
            "[cyan]YES[/]",
            f"{state.yes.shares:>3} shares @ {state.yes.avg_cost:.4f}  |  "
            f"Price: {yes_price:.4f}",
        )
        table.add_row(
            "[magenta]NO[/]",
            f"{state.no.shares:>3} shares @ {state.no.avg_cost:.4f}  |  "
            f"Price: {no_price:.4f}",
        )
        table.add_row(
            "Unrealized PnL:",
            f"[{upnl_color}]{upnl:+.4f} USDC[/]",
        )
        table.add_row("Phase:", phase_label)
        table.add_row("T-minus:", f"[bold]{t_str}[/]")
        if state.emergency_save_count:
            table.add_row(
                "Saves:", f"[bold red]{state.emergency_save_count}[/]"
            )

        return Panel(
            table,
            title=f"[bold]{state.market_type}[/]",
            border_style="blue",
        )

    def _trades_panel(self) -> Panel:
        table = Table(
            show_header=True,
            header_style="bold magenta",
            border_style="dim",
            row_styles=["", "dim"],
        )
        table.add_column("Time", style="dim", width=12)
        table.add_column("Market", width=10)
        table.add_column("Side", width=6)
        table.add_column("Price", justify="right", width=8)
        table.add_column("Shares", justify="right", width=7)
        table.add_column("Phase", width=12)
        table.add_column("PnL", justify="right", width=10)
        table.add_column("Mode", width=8)

        import datetime
        for trade in self._recent_trades:
            ts = trade.get("timestamp", 0)
            dt_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            pnl = float(trade.get("net_pnl", 0))
            pnl_color = "green" if pnl >= 0 else "red"
            mode = "DRY" if trade.get("is_dry_run") else "[red]LIVE[/]"

            table.add_row(
                dt_str,
                str(trade.get("market_type", "")),
                str(trade.get("pair_side", "")),
                f"{float(trade.get('price', 0)):.4f}",
                str(trade.get("shares", 0)),
                str(trade.get("strategy_phase", "")),
                f"[{pnl_color}]{pnl:+.4f}[/]",
                mode,
            )

        return Panel(
            table,
            title="[bold]Recent Trades (last 10)[/]",
            border_style="yellow",
        )
