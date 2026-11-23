"""Pure checks of the guardrail surface: run with `python3 tests/test_rules.py`.

No pytest, no dependency beyond pydantic (config models): the point is to keep
the risk-surface code regression-checked in one command from any environment.
"""

import os
import sys

_ROOT = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_ROOT, "..", "src"))

from worker.config import SessionConfig  # noqa: E402
from worker import rules  # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def now_ctx(minute_of_day, weekday=0, iso="2026-11-23T15:00:00+01:00"):
    return {"minute_of_day": minute_of_day, "weekday": weekday, "iso": iso}


@check("entry window: inside/outside (swing 01:00-22:15 Paris)")
def _():
    cfg = SessionConfig()
    assert rules.in_trading_window(now_ctx(8 * 60), cfg)
    assert rules.in_trading_window(now_ctx(22 * 60 + 15), cfg)
    assert not rules.in_trading_window(now_ctx(0 * 60 + 30), cfg)
    assert not rules.in_trading_window(now_ctx(22 * 60 + 16), cfg)
    assert not rules.in_trading_window(now_ctx(8 * 60, weekday=5), cfg), "Saturday"
    assert not rules.in_trading_window(now_ctx(8 * 60, weekday=6), cfg), "Sunday"


@check("force_window overrides the clock")
def _():
    cfg = SessionConfig(force_window=True)
    assert rules.in_trading_window(now_ctx(3 * 60), cfg)
    assert not rules.weekend_flat_due(now_ctx(22 * 60, weekday=4), cfg)


@check("weekend flat: due Friday 21:45+, not before, not other days")
def _():
    cfg = SessionConfig()
    assert rules.weekend_flat_due(now_ctx(21 * 60 + 45, weekday=4), cfg)
    assert rules.weekend_flat_due(now_ctx(23 * 60, weekday=4), cfg)
    assert not rules.weekend_flat_due(now_ctx(21 * 60 + 44, weekday=4), cfg)
    assert not rules.weekend_flat_due(now_ctx(22 * 60, weekday=1), cfg)


@check("daily stop hits at -1% of the day's start balance")
def _():
    cfg = SessionConfig()
    state = {"balance": 9900.0, "today_net_pnl": -120.0}
    assert rules.day_stop_hit(state, cfg)  # start 10020 -> stop at -100.20
    state2 = {"balance": 9900.0, "today_net_pnl": -50.0}
    assert not rules.day_stop_hit(state2, cfg)
    assert not rules.day_stop_hit({"balance": None, "today_net_pnl": -50.0}, cfg)


@check("cooldown after a losing streak, not after a win")
def _():
    cfg = SessionConfig()  # max_consec_losses=3, pause 240 min
    ctx = now_ctx(15 * 60)
    losing = {"consec_losses": 3, "last_close": "2026-11-23T13:30:00+01:00"}
    assert rules.paused_after_losses(ctx, losing["last_close"], cfg)
    recovered = {"consec_losses": 3, "last_close": "2026-11-23T10:30:00+01:00"}
    assert not rules.paused_after_losses(ctx, recovered["last_close"], cfg)
    assert not rules.paused_after_losses(ctx, None, cfg)


@check("entry_blocked_reason: every blocking guardrail")
def _():
    cfg = SessionConfig()
    ok_state = {"balance": 10000.0, "positions": [], "today_trades": 0,
                "today_net_pnl": 0.0, "consec_losses": 0}
    assert rules.entry_blocked_reason(ok_state, now_ctx(15 * 60), cfg) is None
    assert "unreadable" in rules.entry_blocked_reason(
        {**ok_state, "read_error": "x"}, now_ctx(15 * 60), cfg)
    assert "daily stop" in rules.entry_blocked_reason(
        {**ok_state, "today_net_pnl": -200.0}, now_ctx(15 * 60), cfg)
    assert "max 4 trades/day" in rules.entry_blocked_reason(
        {**ok_state, "today_trades": 4}, now_ctx(15 * 60), cfg)
    assert "open positions" in rules.entry_blocked_reason(
        {**ok_state, "positions": [{"id": 1}, {"id": 2}, {"id": 3}]},
        now_ctx(15 * 60), cfg)
    assert "window" in rules.entry_blocked_reason(ok_state, now_ctx(23 * 60), cfg)
    streak = {**ok_state, "consec_losses": 3,
              "last_close": "2026-11-23T14:30:00+01:00"}
    assert "cooldown" in rules.entry_blocked_reason(streak, now_ctx(15 * 60), cfg)


@check("one position per instrument")
def _():
    state = {"positions": [{"id": 1, "instrument_id": 1}, {"id": 2, "instrument_id": 32}]}
    assert rules.instrument_held(state, 1)
    assert rules.instrument_held(state, 32)
    assert not rules.instrument_held(state, 18)


@check("spread filter at the 0.05% boundary")
def _():
    cfg = SessionConfig()  # max_spread_pct 0.0005
    assert rules.spread_ok(1.10005, 1.10000, cfg)  # 0.000045 relative
    assert not rules.spread_ok(1.10500, 1.10000, cfg)  # ~0.0045
    assert not rules.spread_ok(0.0, 0.0, cfg)


@check("time exit: past max_holding_hours, tolerant to bad timestamps")
def _():
    cfg = SessionConfig()  # 96 h
    ctx = now_ctx(15 * 60, iso="2026-11-23T15:00:00+01:00")
    old = {"id": 1, "opened_at": "2026-11-18T15:00:00+01:00"}  # 5 days: > 96 h
    fresh = {"id": 2, "opened_at": "2026-11-23T14:00:00+01:00"}
    bad = {"id": 3, "opened_at": "not-a-date"}
    none_ = {"id": 4, "opened_at": None}
    out = rules.time_exit_positions([old, fresh, bad, none_], ctx, cfg)
    assert [p["id"] for p in out] == [1]


@check("sizing: risk 0.5% -> units, min exposure, margin cap")
def _():
    cfg = SessionConfig()
    # balance 10000, risk 50 USD, stop distance 0.0150 -> 3333 units @ 1.1050
    sizing, reason = rules.size_trade(10000.0, 1.1050, 1.0900, cfg)
    assert sizing is not None, reason
    assert sizing["units"] == 3333
    assert sizing["leverage"] in cfg.allowed_leverages
    assert sizing["amount_usd"] * sizing["leverage"] >= 10000.0 * 0.25  # exposure sane
    # stop so close that risk-based units explode the margin even at max leverage
    sizing2, reason2 = rules.size_trade(10000.0, 1.1050, 1.1045, cfg)
    assert sizing2 is None and "margin" in reason2
    # zero stop distance
    sizing3, reason3 = rules.size_trade(10000.0, 1.1050, 1.1050, cfg)
    assert sizing3 is None and "stop distance" in reason3


@check("build_order_spec: structural SL, net 2% cap, eToro layout")
def _():
    cfg = SessionConfig()
    sizing = {"leverage": 5, "amount_usd": 2200.0, "units": 3333}
    # buy @1.10, structural stop -1%: closer than the 2% net cap -> structural kept
    spec = rules.build_order_spec(1, "buy", 1.1000, sizing, 1.0890, 1.1220, cfg)
    assert spec == {
        "InstrumentID": 1, "IsBuy": True, "Leverage": 5, "Amount": 2200.0,
        "StopLossRate": 1.089, "TakeProfitRate": 1.122,
    }
    # buy @1.10, structural stop -5%: capped by the 2% net SL
    spec2 = rules.build_order_spec(1, "buy", 1.1000, sizing, 1.0450, 1.2100, cfg)
    assert spec2["StopLossRate"] == 1.078
    # sell @1.10, structural stop above entry: closest of structural / +2%
    spec3 = rules.build_order_spec(32, "sell", 1.1000, sizing, 1.1180, 1.0520, cfg)
    assert spec3["IsBuy"] is False
    assert spec3["StopLossRate"] == 1.118
    assert spec3["TakeProfitRate"] == 1.052


@check("weekly rhythm sanity: window covers all sessions")
def _():
    cfg = SessionConfig()
    assert cfg.window_start_min == 60          # Asia open ~01:00 Paris
    assert cfg.weekdays == (0, 1, 2, 3, 4)
    assert cfg.friday_flat_min < cfg.window_end_min
    assert cfg.max_holding_hours >= 24        # swing: multi-day holds allowed
    assert cfg.daily_stop_pct < 0 and cfg.risk_per_trade <= 0.005


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"ok    - {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  - {name}: {e or 'assertion'}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR - {name}: {type(e).__name__}: {e}")
    else:
        print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
