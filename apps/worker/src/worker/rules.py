"""Pure guardrails: windows, day stop, pauses, limits, swing exits and sizing.

Every function here is deterministic and side-effect free: it computes what
the session may or must do from the account state and the clock context.
This module is the project's core risk surface: change it with care.
"""

import math
from datetime import datetime, timedelta

from worker.config import SessionConfig


def in_trading_window(now_ctx: dict, config: SessionConfig) -> bool:
    """True when an entry is possible (inside the window, on a weekday)."""
    if config.force_window:
        return True
    if now_ctx["weekday"] not in config.weekdays:
        return False
    return config.window_start_min <= now_ctx["minute_of_day"] <= config.last_entry_min


def weekend_flat_due(now_ctx: dict, config: SessionConfig) -> bool:
    """True when everything must be closed before the weekend (Friday evening)."""
    if config.force_window or not config.weekend_flat:
        return False
    return now_ctx["weekday"] == 4 and now_ctx["minute_of_day"] >= config.friday_flat_min


def day_stop_hit(state: dict, config: SessionConfig) -> bool:
    """True when the day's realised P/L reached the daily stop."""
    balance = state.get("balance")
    pnl = state.get("today_net_pnl")
    if balance is None or pnl is None:
        return False
    start_balance = balance - pnl
    if start_balance <= 0:
        return False
    return pnl <= config.daily_stop_pct * start_balance


def paused_after_losses(now_ctx: dict, last_close: str | None, config: SessionConfig) -> bool:
    """True during the cooldown that follows the last loss after a losing streak."""
    if not last_close:
        return False
    last = datetime.fromisoformat(last_close)
    now = datetime.fromisoformat(now_ctx["iso"])
    return now < last + timedelta(minutes=config.pause_after_losses_min)


def entry_blocked_reason(state: dict, now_ctx: dict, config: SessionConfig) -> str | None:
    """Why no new entry is allowed right now, or None when it is."""
    if state.get("read_error"):
        return "account state unreadable"
    if day_stop_hit(state, config):
        return "daily stop reached"
    trades_today = state.get("today_trades", 0) + len(state.get("positions") or [])
    if trades_today >= config.max_trades_per_day:
        return f"max {config.max_trades_per_day} trades/day reached"
    if len(state.get("positions") or []) >= config.max_open_positions:
        return f"max {config.max_open_positions} open positions reached"
    if not in_trading_window(now_ctx, config):
        return "outside the session window"
    streak = state.get("consec_losses", 0)
    if streak >= config.max_consec_losses and paused_after_losses(
        now_ctx, state.get("last_close"), config
    ):
        return f"{streak} consecutive losses: cooldown {config.pause_after_losses_min} min"
    return None


def instrument_held(state: dict, instrument_id: int) -> bool:
    """One position per instrument (config.max_positions_per_instrument == 1)."""
    return any(p.get("instrument_id") == instrument_id for p in state.get("positions") or [])


def spread_ok(ask: float, bid: float, entry_rate: float, stop_rate: float, config: SessionConfig) -> bool:
    """v2a per-trade cost filter: the spread must stay under a fraction of the
    structural stop distance (the cost is capped relative to the risk R)."""
    dist = abs(entry_rate - stop_rate)
    if dist <= 0:
        return False
    return (ask - bid) <= config.max_spread_of_stop * dist


def time_exit_positions(positions: list[dict], now_ctx: dict, config: SessionConfig) -> list[dict]:
    """Positions held too long (opened_at parsing failure keeps the position)."""
    out = []
    now = datetime.fromisoformat(now_ctx["iso"])
    for p in positions or []:
        opened = p.get("opened_at")
        if not opened:
            continue
        try:
            start = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - start > timedelta(hours=config.max_holding_hours):
            out.append(p)
    return out


def size_trade(
    balance: float, entry_rate: float, stop_rate: float, config: SessionConfig
) -> tuple[dict | None, str | None]:
    """(order sizing | None, reason | None): risk-based units under the exposure rule."""
    dist = abs(entry_rate - stop_rate)
    if dist <= 0:
        return None, "non-positive stop distance"
    risk_usd = balance * config.risk_per_trade
    units = math.floor(risk_usd / dist)
    if units <= 0:
        return None, "balance/risk too low for one unit"
    exposure = units * entry_rate
    if exposure < config.min_exposure_usd:
        return None, f"exposure {exposure:.0f} USD below the {config.min_exposure_usd:.0f} USD minimum"
    for lev in config.allowed_leverages:
        margin = exposure / lev
        if margin <= balance * config.margin_cap_ratio:
            return {"leverage": lev, "amount_usd": round(margin, 2), "units": units}, None
    return None, f"margin above {config.margin_cap_ratio:.0%} of the balance even at max leverage"


def build_order_spec(
    instrument_id: int, direction: str, entry_rate: float, sizing: dict,
    stop_rate: float, tp_rate: float, config: SessionConfig,
) -> dict:
    """The body of a market-open order, eToro public API layout."""
    is_buy = direction == "buy"
    # Net broker SL: the tighter of the structural stop and the 2 % net.
    # (If the structural stop sits beyond 2 %, the net caps it. If eToro's own
    # minimum SL distance is wider than what we ask, eToro widens it itself.)
    max_sl = entry_rate * (1 - config.broker_sl_pct if is_buy else 1 + config.broker_sl_pct)
    if is_buy:
        sl = max(stop_rate, max_sl)      # both sit below entry: keep the closest
    else:
        sl = min(stop_rate, max_sl)      # both sit above entry: keep the closest
    return {
        "InstrumentID": instrument_id,
        "IsBuy": is_buy,
        "Leverage": sizing["leverage"],
        "Amount": sizing["amount_usd"],
        "StopLossRate": round(sl, 6),
        "TakeProfitRate": round(tp_rate, 6),
    }
