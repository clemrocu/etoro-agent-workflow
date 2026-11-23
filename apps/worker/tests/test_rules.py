"""Pure stdlib checks of the guardrail surface: run with `python3 tests/test_rules.py`.

No pytest, no dependency: the point is to keep the risk-surface code regression-checked
in one command from any environment.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "py"))

from worker.config import SessionConfig  # noqa: E402
from worker import rules  # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def now_ctx(minute_of_day, weekday=0):
    return {"minute_of_day": minute_of_day, "weekday": weekday, "iso": "2026-11-23T15:00:00+01:00"}


@check("entry window: inside/outside")
def _():
    cfg = SessionConfig()
    assert rules.in_trading_window(now_ctx(14 * 60 + 45), cfg)
    assert not rules.in_trading_window(now_ctx(12 * 60), cfg)
    assert not rules.in_trading_window(now_ctx(16 * 60 + 10), cfg)
    assert not rules.in_trading_window(now_ctx(15 * 60), weekday=5)


@check("force_window overrides the clock")
def _():
    cfg = SessionConfig(force_window=True)
    assert rules.in_trading_window(now_ctx(3 * 60), cfg)


@check("forced close boundary")
def _():
    cfg = SessionConfig()
    assert not rules.past_force_close(now_ctx(16 * 60), cfg)
    assert rules.past_force_close(now_ctx(16 * 60 + 15), cfg)
    assert rules.past_force_close(now_ctx(23 * 60), cfg)


@check("daily stop hits at -1% of the day's start balance")
def _():
    cfg = SessionConfig()
    state = {"balance": 9900.0, "today_net_pnl": -120.0}
    assert rules.day_stop_hit(state, cfg)  # start 10020 -> stop at -100.20
    state2 = {"balance": 9900.0, "today_net_pnl": -50.0}
    assert not rules.day_stop_hit(state2, cfg)
    assert not rules.day_stop_hit({"balance": None, "today_net_pnl": -50.0}, cfg)


@check("max positions blocks entry")
def _():
    cfg = SessionConfig()
    state = {"positions": [{"id": 1}, {"id": 2}], "today_trades": 0, "consec_losses": 0}
    reason = rules.entry_blocked_reason(state, now_ctx(15 * 60), cfg)
    assert reason and "open positions" in reason


@check("max trades/day blocks entry")
def _():
    cfg = SessionConfig()
    state = {"positions": [], "today_trades": 15, "consec_losses": 0}
    reason = rules.entry_blocked_reason(state, now_ctx(15 * 60), cfg)
    assert reason and "trades" in reason


@check("consecutive-loss cooldown blocks then releases")
def _():
    cfg = SessionConfig()
    close_iso = "2026-11-23T15:40:00+01:00"
    state = {"positions": [], "today_trades": 1, "consec_losses": 3, "last_close": close_iso}
    assert rules.paused_after_losses(now_ctx(15 * 60), close_iso, cfg)
    assert not rules.paused_after_losses(now_ctx(16 * 30), close_iso, cfg)
    reason = rules.entry_blocked_reason(state, now_ctx(15 * 60), cfg)
    assert reason and "cooldown" in reason
    reason2 = rules.entry_blocked_reason(state, now_ctx(16 * 30), cfg)
    assert reason2 is None


@check("soft stop detection, long and short")
def _():
    cfg = SessionConfig()
    long_pos = {"id": 1, "is_buy": True, "open_rate": 1.1000, "units": 1000}
    short_pos = {"id": 2, "is_buy": False, "open_rate": 1.1000, "units": 1000}
    hits = rules.soft_stop_hits([long_pos, short_pos], ask=1.1017, bid=1.0996, config=cfg)
    assert [h["id"] for h in hits] == [2]  # short hit: ask >= open + 6 pips; long survives: bid > open - 6 pips
    hits2 = rules.soft_stop_hits([long_pos], ask=1.1050, bid=1.1048, config=cfg)
    assert hits2 == []


@check("sizing: risk 0.5% of 100k, soft SL 6 pips -> units, exposure minimum, margin cap")
def _():
    cfg = SessionConfig()
    sizing, reason = rules.size_trade(100_000.0, 1.09, cfg)
    assert sizing is not None, reason
    assert sizing["units"] == 833  # 500 USD / 0.0006 -> 833.3.. floor
    exposure = 833 * 1.09
    assert sizing["leverage"] == 1  # exposure ~908 > 50% of 100k only if... check explicit
    assert sizing["amount_usd"] == round(exposure / sizing["leverage"], 2)
    tiny, reason2 = rules.size_trade(150.0, 1.09, cfg)
    assert tiny is None and "exposure" in reason2


@check("order spec: TP/SL on the right side of the entry")
def _():
    cfg = SessionConfig()
    spec = rules.build_order_spec("buy", 1.0950, {"leverage": 5, "amount_usd": 200.0, "units": 833}, cfg)
    assert spec["IsBuy"] is True and spec["InstrumentID"] == 1
    assert spec["TakeProfitRate"] == round(1.0960, 5)
    assert spec["StopLossRate"] == round(1.0950 * 0.99, 5)
    spec2 = rules.build_order_spec("sell", 1.0950, {"leverage": 5, "amount_usd": 200.0, "units": 833}, cfg)
    assert spec2["TakeProfitRate"] == round(1.0940, 5)
    assert spec2["StopLossRate"] == round(1.0950 * 1.01, 5)


@check("spread filter")
def _():
    cfg = SessionConfig()
    assert rules.spread_ok(1.09607, 1.09599, cfg)  # 0.8 pips
    assert not rules.spread_ok(1.0975, 1.0950, cfg)


def main():
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {e}")
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
