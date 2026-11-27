"""Pullback swing strategy, pure functions, stdlib only.

Context (H1): trade only with the higher-frame trend. Entry (M15): after a
pullback INTO the H1 moving-average zone, on a resumption candle. Exits are
structure-based: stop under/over the M15 swing, take profit at a fixed R
multiple. Costs are capped by design: one candidate per poll, no re-entry on
an instrument that already holds a position, no order outside liquid sessions.

Signal contract (returned dict, all values rounded by the caller):
    {instrument_id, direction: "buy"|"sell", entry, stop, tp, score, reason}
"""

import statistics

UNIVERSE: dict[int, str] = {
    1: "EURUSD", 2: "GBPUSD", 3: "NZDUSD", 4: "USDCAD", 5: "USDJPY",
    7: "AUDUSD", 17: "OIL", 18: "GOLD", 19: "SILVER", 27: "SPX500",
    28: "NSDQ100", 29: "DJ30", 30: "UK100", 31: "FRA40", 32: "GER40",
    33: "AUS200", 36: "JPN225",
}

# v2a two-pass scan: how far from the H1 EMA20 zone the last H1 close may sit
# before the (paid) M15 read is skipped. Generous on purpose: the pre-filter
# must never drop an instrument the full analyze() would have signalled.
NEAR_ZONE_ATR_H1 = 4.0


def ema(values: list[float], period: int) -> list[float]:
    """Classic EMA, oldest first, same length as the input."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out, prev = [], None
    for v in values:
        prev = float(v) if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr(candles: list[dict], period: int = 14) -> float | None:
    """Wilder ATR of the latest candles (oldest first)."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    return a


def h1_trend(h1: list[dict]) -> str | None:
    """"up" / "down" / None from H1 EMA alignment. Needs >= 210 bars."""
    if len(h1) < 210:
        return None
    closes = [c["close"] for c in h1]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    last = closes[-1]
    if e20[-1] > e50[-1] > e200[-1] and last > e200[-1]:
        return "up"
    if e20[-1] < e50[-1] < e200[-1] and last < e200[-1]:
        return "down"
    return None


def trend_strength(h1: list[dict]) -> float:
    """Normalized EMA20 slope of the last 6 H1 bars (0 when flat/unknown)."""
    if len(h1) < 26:
        return 0.0
    closes = [c["close"] for c in h1]
    e20 = ema(closes, 20)
    base = abs(e20[-1]) or 1.0
    return (e20[-1] - e20[-6]) / base


def _pullback_zone(side: str, h1: list[dict], a_m15: float) -> tuple[float, float]:
    """(far, near) values for the pullback test: far = invalidation edge the
    closes must not cross, near = touch edge the wicks must reach.

    Uptrend: price above the EMA, pullback dips down; the wick must touch
    e20 + 0.4 ATR and no close may sit below e20 - 1.5 ATR. Downtrend: the
    exact mirror (wick up to e20 - 0.4 ATR, no close above e20 + 1.5 ATR).
    """
    closes = [c["close"] for c in h1]
    e20 = ema(closes, 20)[-1]
    if side == "up":
        return e20 - 1.5 * a_m15, e20 + 0.4 * a_m15
    return e20 + 1.5 * a_m15, e20 - 0.4 * a_m15


def near_pullback_zone(h1: list[dict]) -> bool:
    """Cheap H1-only pre-filter for the v2a two-pass scan: True when the H1
    trend is aligned and the last close sits within ~NEAR_ZONE_ATR_H1 x ATR(H1)
    of the EMA20 zone, i.e. when the M15 read can still produce a signal."""
    side = h1_trend(h1)
    if side is None:
        return False
    a = atr(h1)
    if a is None or a <= 0:
        return True  # unreadable volatility: stay conservative, read M15
    closes = [c["close"] for c in h1]
    e20 = ema(closes, 20)[-1]
    return abs(closes[-1] - e20) <= NEAR_ZONE_ATR_H1 * a


def analyze(instrument_id: int, m15: list[dict], h1: list[dict], tp_r: float) -> dict | None:
    """One instrument: a pullback signal or None. Pure, stateless, no i/o."""
    if len(m15) < 30 or len(h1) < 210:
        return None
    side = h1_trend(h1)
    if side is None:
        return None
    a = atr(m15)
    if a is None or a <= 0:
        return None
    far, near = _pullback_zone("up" if side == "up" else "down", h1, a)

    # The pullback must have HAPPENED in the last 3 closed M15 bars...
    window = m15[-3:]
    touched = any(
        (b["low"] <= near if side == "up" else b["high"] >= near)
        for b in window
    )
    if not touched:
        return None
    # ...and did not fall apart: no close beyond the far edge of the zone.
    broke = any(
        (b["close"] < far if side == "up" else b["close"] > far)
        for b in window
    )
    if broke:
        return None

    # ...ending with a resumption candle on the LAST closed bar.
    last, prev = m15[-1], m15[-2]
    up_candle = last["close"] > last["open"] and last["close"] > prev["close"]
    down_candle = last["close"] < last["open"] and last["close"] < prev["close"]
    rng = (last["high"] - last["low"]) or 1e-12
    close_pos = (last["close"] - last["low"]) / rng
    if side == "up":
        if not (up_candle and close_pos >= 0.6):
            return None
    else:
        if not (down_candle and close_pos <= 0.4):
            return None

    entry = last["close"]
    swing = m15[-8:]
    if side == "up":
        stop = min(b["low"] for b in swing) - 0.5 * a
    else:
        stop = max(b["high"] for b in swing) + 0.5 * a
    dist = (entry - stop) if side == "up" else (stop - entry)
    # Sanity: a usable stop, but not a scalping-sized one either.
    if dist <= 0 or dist > 0.05 * entry:
        return None
    # The stop must be near enough to be meaningful, far enough from noise.
    if dist < max(0.004 * entry, 1.5 * a):
        return None
    tp = entry + tp_r * dist if side == "up" else entry - tp_r * dist

    depth = statistics.mean(
        (near - b["low"]) / a if side == "up" else (b["high"] - near) / a
        for b in window
    )
    score = round(depth + max(0.0, trend_strength(h1) * 40), 4)
    direction = "buy" if side == "up" else "sell"
    return {
        "instrument_id": instrument_id,
        "symbol": UNIVERSE.get(instrument_id, str(instrument_id)),
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "tp": tp,
        "score": score,
        "reason": (
            f"{UNIVERSE.get(instrument_id, instrument_id)} pullback "
            f"{'up' if side == 'up' else 'down'}-trend resumption candle"
        ),
    }


def decide_markets(
    snapshots: dict[int, dict], held_instruments: set[int], tp_r: float
) -> list[dict]:
    """All valid candidates across the universe, best score first.

    ``snapshots``: {instrument_id: {"m15": [...], "h1": [...]}}.
    ``held_instruments``: instruments with an open position (no re-entry).
    """
    candidates = []
    for iid, snap in (snapshots or {}).items():
        if not snap or iid in (held_instruments or set()):
            continue
        sig = analyze(iid, snap.get("m15") or [], snap.get("h1") or [], tp_r)
        if sig:
            candidates.append(sig)
    candidates.sort(key=lambda s: s["score"], reverse=True)
    return candidates
