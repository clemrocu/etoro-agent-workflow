"""Pure guardrails: window, day stop, pauses, limits, soft SL and sizing.

Every function here is deterministic and side-effect free: it computes what the
session may or must do from the account state and the clock context returned by
activities. This module is the project's core risk surface: change it with care.
"""

import math

from worker.config import SessionConfig

PIP = 0.0001


def in_trading_window(now_ctx: dict, config: SessionConfig) -> bool:
    """True when an entry is possible (inside the window, on a weekday)."""
    if config.force_window:
        return True
    if now_ctx["weekday"] not in config.weekdays:
        return False
    return config.window_start_min <= now_ctx["minute_of_day"] <= config.last_entry_min


def past_force_close(now_ctx: dict, config: SessionConfig) -> bool:
    if config.force_window:
        return False
    return now_ctx["minute_of_day"] >= config.force_close_min


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
    from datetime import datetime, timedelta

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
        return "outside the entry window"
    streak = state.get("consec_losses", 0)
    if streak >= config.max_consec_losses and paused_after_losses(
        now_ctx, state.get("last_close"), config
    ):
        return f"{streak} consecutive losses: cooldown {config.pause_after_losses_min} min"
    return None


def soft_stop_hits(positions: list[dict], ask: float, bid: float, config: SessionConfig) -> list[dict]:
    """Positions whose soft stop level is breached at the current price."""
    out = []
    for p in positions or []:
        if p.get("is_buy"):
            soft = p["open_rate"] - config.soft_sl_pips * PIP
            hit = bid <= soft
        else:
            soft = p["open_rate"] + config.soft_sl_pips * PIP
            hit = ask >= soft
        if hit:
            out.append({**p, "soft_rate": round(soft, 5)})
    return out


def size_trade(balance: float, entry_rate: float, config: SessionConfig) -> tuple[dict | None, str | None]:
    """(order sizing | None, reason | None): risk-based units under the exposure rule."""
    risk_usd = balance * config.risk_per_trade
    units = math.floor(risk_usd / (config.soft_sl_pips * PIP))
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


def spread_ok(ask: float, bid: float, config: SessionConfig) -> bool:
    return (ask - bid) / PIP <= config.spread_max_pips


def build_order_spec(direction: str, entry_rate: float, sizing: dict, config: SessionConfig) -> dict:
    """The body of a market-open order, eToro public API layout."""
    is_buy = direction == "buy"
    tp = round(entry_rate + (config.tp_pips * PIP if is_buy else -config.tp_pips * PIP), 5)
    sl = round(entry_rate * (1 - config.broker_sl_pct if is_buy else 1 + config.broker_sl_pct), 5)
    return {
        "InstrumentID": config.instrument_id,
        "IsBuy": is_buy,
        "Leverage": sizing["leverage"],
        "Amount": sizing["amount_usd"],
        "StopLossRate": sl,
        "TakeProfitRate": tp,
    }
