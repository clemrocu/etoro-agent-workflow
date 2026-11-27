"""Synthetic checks of the pullback swing signal: run `python3 tests/test_pullback.py`.

Pure stdlib, deterministic candles built around the module's own zone math
(so a zone-parameter change moves the fixtures with it, not against it).
"""

import os
import sys

_ROOT = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_ROOT, "..", "src"))

from worker import pullback  # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def ramp_h1(n=220, start=100.0, slope=0.1):
    """Monotone H1 ramp: EMA20 > EMA50 > EMA200 guaranteed for slope > 0."""
    return [candle(c, c + 0.01, c - 0.01, c) for c in (start + slope * i for i in range(n))]


def calm_m15(base, n=30, step=0.02, rng=0.06):
    """Quietly rising M15 bars far from any zone edge."""
    return [candle(b, b + rng / 2, b - rng / 2, b + step) for b in (base + step * i for i in range(n))]


def pullback_scenario(side="up"):
    """Build one valid pullback around the true EMA20 of the H1 ramp."""
    sign = 1.0 if side == "up" else -1.0
    h1 = ramp_h1(slope=0.1 * sign)
    e20 = pullback.ema([c["close"] for c in h1], 20)[-1]
    m15 = calm_m15(e20 * 1.004, n=30)
    a = pullback.atr(m15)
    # Three pullback bars: dip wick INTO the zone, closes holding the near edge,
    # then (appended later) the resumption candle. ATR stays tiny.
    if side == "up":
        m15 += [
            candle(e20 - 0.02, e20 + 0.02, e20 - 0.30, e20 + 0.01),
            candle(e20 + 0.01, e20 + 0.02, e20 - 1.00, e20 + 0.02),
            candle(e20 + 0.02, e20 + 0.02, e20 - 0.30, e20 + 0.03),
        ]
    else:
        m15 = calm_m15(e20 * 0.996, n=30, step=-0.02)
        m15 += [
            candle(e20 + 0.02, e20 + 0.30, e20 - 0.02, e20 - 0.01),
            candle(e20 - 0.01, e20 + 1.00, e20 - 0.02, e20 - 0.02),
            candle(e20 - 0.02, e20 + 0.30, e20 - 0.02, e20 - 0.03),
        ]
    return h1, m15, e20


@check("uptrend + pullback + resumption -> long signal, 2R geometry")
def _():
    h1, m15, e20 = pullback_scenario("up")
    sig = pullback.analyze(1, m15, h1, tp_r=2.0)
    assert sig is not None, "no signal on a textbook long pullback"
    res = candle(e20 + 0.03, e20 + 0.34, e20 + 0.02, e20 + 0.30)  # close>open>prev
    m15 = m15 + [res]
    sig = pullback.analyze(1, m15, h1, tp_r=2.0)
    assert sig is not None, f"no signal after the resumption candle (e20={e20:.4f})"
    assert sig["direction"] == "buy"
    assert sig["symbol"] == "EURUSD"
    dist = sig["entry"] - sig["stop"]
    assert dist > 0
    assert abs(sig["tp"] - (sig["entry"] + 2.0 * dist)) < 1e-9, "TP not at 2R"
    assert sig["score"] > 0
    return sig


@check("downtrend mirror -> short signal")
def _():
    h1, m15, e20 = pullback_scenario("down")
    res = candle(e20 - 0.03, e20 + 0.02, e20 - 0.34, e20 - 0.30)
    m15 = m15 + [res]
    sig = pullback.analyze(32, m15, h1, tp_r=2.0)
    assert sig is not None
    assert sig["direction"] == "sell"
    dist = sig["stop"] - sig["entry"]
    assert dist > 0
    assert abs(sig["tp"] - (sig["entry"] - 2.0 * dist)) < 1e-9


@check("no signal without the H1 trend")
def _():
    h1 = [candle(100, 100.05, 99.95, 100) for _ in range(220)]  # flat: EMAs equal
    e20 = 100.0
    m15 = calm_m15(100.2, n=24)
    m15 += [candle(100.21, 100.5, 99.4, 100.3)]
    a = pullback.atr(m15)
    assert pullback.h1_trend(h1) is None
    assert pullback.analyze(1, m15, h1, 2.0) is None
    assert e20 == 100.0


@check("pullback without a resumption candle -> no entry")
def _():
    h1, m15, e20 = pullback_scenario("up")
    last = m15[-1]
    stall = candle(last["close"], last["close"] + 0.2, last["low"], last["close"] - 0.05)
    m15 = m15 + [stall]  # close < open: not a resumption
    assert pullback.analyze(1, m15, h1, 2.0) is None


@check("stop too close (inside the noise floor) -> rejected")
def _():
    h1 = ramp_h1()
    e20 = pullback.ema([c["close"] for c in h1], 20)[-1]
    # Shallow pullback (low just inside the zone) but a high swing low:
    # stop distance lands under max(0.4% of price, 1.5*ATR).
    m15 = calm_m15(e20 + 0.05, n=30)
    m15 += [candle(e20 + 0.06, e20 + 0.08, e20 + 0.01, e20 + 0.07)]
    assert pullback.analyze(1, m15, h1, 2.0) is None


@check("two-pass pre-filter: near_pullback_zone gates the M15 read")
def _():
    flat = [candle(100, 100.05, 99.95, 100) for _ in range(220)]
    assert pullback.near_pullback_zone(flat) is False          # no aligned trend
    gentle = ramp_h1(slope=0.005)                              # price hugging the EMA20 zone
    assert pullback.near_pullback_zone(gentle) is True
    extended = ramp_h1(slope=0.1)                              # price far above the zone
    assert pullback.near_pullback_zone(extended) is False
    assert pullback.near_pullback_zone([]) is False            # too short: no trend, no fetch


@check("decide_markets: held instrument skipped, best score first")
def _():
    h1, m15, e20 = pullback_scenario("up")
    res = candle(e20 + 0.03, e20 + 0.34, e20 + 0.02, e20 + 0.30)
    snap = {"m15": m15 + [res], "h1": h1}
    markets = {1: snap, 2: snap}
    held_empty = pullback.decide_markets(markets, set(), 2.0)
    assert len(held_empty) == 2
    assert held_empty[0]["score"] >= held_empty[1]["score"]
    held_one = pullback.decide_markets(markets, {held_empty[0]["instrument_id"]}, 2.0)
    assert len(held_one) == 1
    assert held_one[0]["instrument_id"] != held_empty[0]["instrument_id"]
    assert pullback.decide_markets({}, set(), 2.0) == []


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
