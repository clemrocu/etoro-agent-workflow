"""Session configuration model: every session parameter is explicit and overridable.

Defaults implement the project guardrails: window 14h30-16h30 Paris (minute-of-day),
last entry 16h05, forced close 16h15, max 2 open positions, 15 trades/day,
daily stop -1 %, soft SL 6 pips, broker SL net 1 %, risk 0.5 % per trade.
"""

from pydantic import BaseModel, Field


class SessionConfig(BaseModel):
    # identity
    session_name: str = "etoro-agent"
    dry_run: bool = False          # never send any order
    force_window: bool = False     # test mode: ignore the clock window

    # market
    instrument_id: int = 1         # EUR/USD

    # session window (minute of day, Paris time)
    window_start_min: int = 14 * 60 + 30
    window_end_min: int = 16 * 60 + 30
    last_entry_min: int = 16 * 60 + 5
    force_close_min: int = 16 * 60 + 15
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday..Friday

    # guardrails
    max_open_positions: int = 2
    max_trades_per_day: int = 15
    daily_stop_pct: float = -0.01
    max_consec_losses: int = 3
    pause_after_losses_min: int = 30
    spread_max_pips: float = 1.5

    # per-trade risk
    risk_per_trade: float = 0.005
    soft_sl_pips: float = 6.0
    tp_pips: float = 10.0
    broker_sl_pct: float = 0.01
    margin_cap_ratio: float = 0.5
    min_exposure_usd: float = 1000.0
    allowed_leverages: tuple[int, ...] = (1, 2, 5, 10, 20, 30)
