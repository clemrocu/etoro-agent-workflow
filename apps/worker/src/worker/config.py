"""Swing session configuration: every parameter is explicit and overridable.

v2 defaults implement the swing pullback strategy guardrails (verified universe,
all sessions Europe/US/Asia). Project guardrails stay in force: daily stop -1 %,
risk 0.5 % per trade, halts on failure. Defaults per validated UI choices of
2026-11-23: real agent portfolio VibeBot1 (virtual balance 10 000 USD, mirrored
at 5 % into the caller's account / mirrorId 11759623), token in ETORO_TOKEN.
"""

from pydantic import BaseModel, Field

# Validated eToro instrument ids (eToro Public API, 2026-11-23 live checks,
# all allowOpen, fractional units, min position exposure 1000 USD):
# FX majors + metals + energy + indices covering the Asia/Europe/US sessions.
# v2a (2026-11-27): NZDUSD, OIL and SPX500 added (validated 2026-11-23,
# see knowledge bot-ia-etoro/univers-et-couts.md); PLATINUM stays excluded
# by the cost verdict.
DEFAULT_INSTRUMENTS: tuple[int, ...] = (
    1,    # EURUSD
    2,    # GBPUSD
    3,    # NZDUSD
    4,    # USDCAD
    5,    # USDJPY
    7,    # AUDUSD
    17,   # OIL (WTI crude)
    18,   # GOLD
    19,   # SILVER
    27,   # SPX500
    28,   # NSDQ100
    29,   # DJ30
    30,   # UK100
    31,   # FRA40
    32,   # GER40
    33,   # AUS200
    36,   # JPN225
)


class SessionConfig(BaseModel):
    # identity
    session_name: str = "etoro-agent-swing"
    dry_run: bool = True            # default ON: no order is ever sent until dry_run=false
    force_window: bool = False       # test mode: ignore the clock window

    # market universe (all sessions: Asia opens ~01:00 Paris, US closes ~22:30)
    instruments: tuple[int, ...] = DEFAULT_INSTRUMENTS

    # session window (minute of day, Paris time), Monday..Friday
    window_start_min: int = 1 * 60          # 01:00 (Tokyo open)
    last_entry_min: int = 22 * 60 + 15      # 22:15 (last US entries)
    window_end_min: int = 22 * 60 + 30      # 22:30 (US close, poll ends)
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    # weekend flat: no position is held through the weekend
    weekend_flat: bool = True
    friday_flat_min: int = 21 * 60 + 45     # 21:45 Paris Friday: close everything

    # day guardrails
    max_open_positions: int = 3
    max_positions_per_instrument: int = 1
    max_trades_per_day: int = 4
    daily_stop_pct: float = -0.01
    max_consec_losses: int = 3
    pause_after_losses_min: int = 240       # swing: 4h cooldown after a streak

    # per-trade risk (agent virtual balance 10 000 USD)
    risk_per_trade: float = 0.005
    tp_r_multiple: float = 2.0             # take profit at 2x the stop distance
    broker_sl_pct: float = 0.02             # net broker SL 2 % (swing distances)
    max_holding_hours: int = 96              # time exit: no position older than 4 days
    margin_cap_ratio: float = 0.34
    min_exposure_usd: float = 1000.0
    allowed_leverages: tuple[int, ...] = (1, 2, 5, 10, 20)

    # costs (v2a, 2026-11-27): per-trade filter, the spread must stay under
    # a fraction of the structural stop distance R (replaces the flat
    # 0.05 %-of-price cap: the cost only makes sense relative to the risk)
    max_spread_of_stop: float = 0.10        # (ask-bid) <= 10 % of |entry - stop|
