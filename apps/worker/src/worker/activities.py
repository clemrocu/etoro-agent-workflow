"""Socle activities: clock, account state, markets, decision, execution, notify.

All non-deterministic or side-effecting work lives here (Temporal sandbox): the
workflow bodies only orchestrate. Order-sending activities are single-attempt
(retry_policy_max_attempts=1) and own their idempotent retry, so Temporal replay
can never double-fire an order. Activities never log credentials.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog
from mistralai import workflows
from pydantic import BaseModel

from env.etoro import env as etoro_env
from worker import etoro as etoro_lib
from worker import rules, strategy
from worker.config import SessionConfig

TIMEOUT = timedelta(seconds=120)
logger = structlog.get_logger("etoro-agent")

PARIS = etoro_lib.PARIS
M15 = etoro_lib.M15
H1 = etoro_lib.H1
CANDLES_M15 = 60
CANDLES_H1 = 210          # EMA200 needs 200 closed H1 bars + warm-up margin
SCAN_PAUSE_S = 0.25       # pacing between instrument reads (shared 60 req/min)


# ------------------------------------------------------------------
# clock
# ------------------------------------------------------------------
@workflows.activity(start_to_close_timeout=TIMEOUT)
async def now_context() -> dict[str, Any]:
    """Everything the deterministic workflow body needs to know about time."""
    now = datetime.now(PARIS)
    return {
        "iso": now.isoformat(),
        "minute_of_day": now.hour * 60 + now.minute,
        "weekday": now.weekday(),
        "date": now.date().isoformat(),
        "utc_iso": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------
# reads
# ------------------------------------------------------------------
def _day_stats(trades: list[dict] | None) -> dict[str, Any]:
    if trades is None:
        return {}
    pnl = round(sum(t["net"] for t in trades), 2)
    streak = 0
    for t in reversed(trades):
        if t["net"] < 0:
            streak += 1
        else:
            break
    return {
        "today_trades": len(trades),
        "today_net_pnl": pnl,
        "consec_losses": streak,
        "last_close": trades[-1]["closed"] if trades else None,
    }


@workflows.activity(start_to_close_timeout=TIMEOUT)
async def read_account_state(config: SessionConfig) -> dict[str, Any]:
    """Single-source-of-truth account snapshot: balance, positions, day stats."""
    if not etoro_env.has_auth():
        return {"read_error": "no eToro credential configured (ETORO_TOKEN or keys)"}
    client = etoro_lib.EtoroClient()
    balance = await client.get_balance()
    positions = await client.get_positions()
    trades = await client.get_today_trades()
    if balance is None or positions is None:
        return {"read_error": "balance or portfolio unreadable", "positions": [], "balance": None,
                "today_trades": 0, "today_net_pnl": 0.0, "consec_losses": 0, "last_close": None}
    state = {
        "balance": balance,
        "positions": positions,
        **_day_stats(trades),
    }
    if trades is None:
        state["read_error"] = "trade history unreadable"
    return state


@workflows.activity(start_to_close_timeout=TIMEOUT)
async def read_markets(config: SessionConfig) -> dict[int, dict[str, Any]]:
    """{instrument_id: {ask, bid, m15: [...], h1: [...]}} for the whole universe.

    One batched rates call, then paced per-instrument candle reads; a failing
    instrument yields a snapshot with a read_error instead of blocking the scan.
    """
    if not etoro_env.has_auth():
        return {}
    client = etoro_lib.EtoroClient()
    ids = list(config.instruments)
    rates = await client.get_rates(ids)
    markets: dict[int, dict[str, Any]] = {}
    for iid in ids:
        snap: dict[str, Any] = {}
        if iid in rates:
            snap["ask"], snap["bid"] = rates[iid]
        else:
            snap["read_error"] = "rate unreadable"
        m15 = await client.get_candles(iid, "FifteenMinutes", CANDLES_M15, M15)
        h1 = await client.get_candles(iid, "OneHour", CANDLES_H1, H1)
        if m15 is None or h1 is None:
            snap["read_error"] = snap.get("read_error") or "candles unreadable"
        snap["m15"] = m15 or []
        snap["h1"] = h1 or []
        markets[iid] = snap
        await asyncio.sleep(SCAN_PAUSE_S)
    return markets


# ------------------------------------------------------------------
# decision (strategy + guardrails)
# ------------------------------------------------------------------
@workflows.activity(start_to_close_timeout=TIMEOUT)
async def evaluate(
    config: SessionConfig, state: dict[str, Any], markets: dict[int, dict[str, Any]], now_ctx: dict[str, Any]
) -> dict[str, Any]:
    """Strategy decision under the guardrails: {action: open|skip, reason, order_spec?}."""
    blocked = rules.entry_blocked_reason(state, now_ctx, config)
    if blocked:
        return {"action": "skip", "reason": f"guardrail: {blocked}"}
    if not markets:
        return {"action": "skip", "reason": "guardrail: markets unreadable"}
    decision = strategy.decide(markets, state, config)
    if decision.action == "skip":
        return {"action": "skip", "reason": decision.reason}
    snap = markets.get(decision.instrument_id) or {}
    if snap.get("read_error"):
        return {"action": "skip", "reason": f"guardrail: market of {decision.symbol} unreadable"}
    ask, bid = snap["ask"], snap["bid"]
    if not rules.spread_ok(ask, bid, config):
        spread_pct = (ask - bid) / ((ask + bid) / 2)
        return {"action": "skip",
                "reason": f"guardrail: {decision.symbol} spread {spread_pct:.4%} too wide"}
    direction = decision.direction or "buy"
    entry_rate = ask if direction == "buy" else bid
    sizing, reason = rules.size_trade(state["balance"], entry_rate, decision.stop_rate, config)
    if sizing is None:
        return {"action": "skip", "reason": f"guardrail: {reason}"}
    order_spec = rules.build_order_spec(
        decision.instrument_id or 0, direction, entry_rate, sizing,
        decision.stop_rate, decision.tp_rate, config,
    )
    return {
        "action": "open",
        "instrument_id": decision.instrument_id,
        "symbol": decision.symbol,
        "direction": direction,
        "entry_rate": entry_rate,
        "sizing": sizing,
        "order_spec": order_spec,
        # known ids: the post-order failsafe must only match NEW positions
        "known_position_ids": [p["id"] for p in state.get("positions") or []],
    }


# ------------------------------------------------------------------
# execution
# ------------------------------------------------------------------
class OrderResult(BaseModel):
    ok: bool = False
    order_id: str | None = None
    position_id: int | None = None
    halt_session: bool = False
    detail: str = ""


@workflows.activity(start_to_close_timeout=TIMEOUT, retry_policy_max_attempts=1)
async def place_entry(config: SessionConfig, decision: dict[str, Any]) -> OrderResult:
    """Single-attempt activity: owns its idempotent retry with the same x-request-id."""
    import uuid as uuidlib

    client = etoro_lib.EtoroClient()
    url_path = client._routes["market_open"]
    body = decision["order_spec"]
    rid = str(uuidlib.uuid4())
    status, data = await client.call("POST", url_path, body=body, request_id=rid)
    if status in etoro_lib.RETRYABLE:
        await asyncio.sleep(4)
        status, data = await client.call("POST", url_path, body=body, request_id=rid)
    if status != 200:
        # Failsafe: a timeout may still have opened the position; look for it.
        known = set(decision.get("known_position_ids") or [])
        found = await _detect_new_position(client, known, deadline=8)
        if found:
            await _notify_http(
                title="eToro agent: order in timeout, position opened",
                message=(f"Position {found['id']} on {decision.get('symbol')} visible "
                         f"after HTTP {status}. Check the account."),
                priority="high", tags="warning",
            )
            return OrderResult(ok=True, position_id=found["id"], halt_session=True,
                               detail=f"opened after HTTP {status}")
        return OrderResult(ok=False, halt_session=True,
                           detail=f"order rejected: HTTP {status} {str(data)[:180]}")
    order_id = ((data or {}).get("orderForOpen") or {}).get("orderID")
    found = await _detect_new_position(client, set(), deadline=15)
    await _notify_http(
        title="eToro agent: position opened",
        message=(f"{decision['direction'].upper()} {decision.get('symbol')} "
                 f"{decision['sizing']['units']} units @ ~{decision['entry_rate']:.5f} "
                 f"(order {order_id}). Structural TP/SL attached."),
        priority="default", tags="chart_with_upwards_trend",
    )
    return OrderResult(ok=True, order_id=order_id, position_id=found["id"] if found else None,
                       detail=f"order {order_id} accepted")


async def _detect_new_position(client, pre_ids: set, deadline: float = 15):
    end = asyncio.get_running_loop().time() + deadline
    while asyncio.get_running_loop().time() < end:
        positions = await client.get_positions()
        if positions:
            new = [p for p in positions if p["id"] not in pre_ids]
            if new:
                return max(new, key=lambda p: p["id"])
        await asyncio.sleep(2.5)
    return None


@workflows.activity(start_to_close_timeout=TIMEOUT, retry_policy_max_attempts=1)
async def close_position(config: SessionConfig, position: dict[str, Any], reason: str) -> OrderResult:
    import uuid as uuidlib

    client = etoro_lib.EtoroClient()
    path = client._routes["market_close"].format(position_id=position["id"])
    body = {"InstrumentID": position["instrument_id"]}
    rid = str(uuidlib.uuid4())
    status, data = await client.call("POST", path, body=body, request_id=rid)
    if status in etoro_lib.RETRYABLE:
        await asyncio.sleep(4)
        status, data = await client.call("POST", path, body=body, request_id=rid)
    if status != 200:
        return OrderResult(ok=False, halt_session=True,
                          detail=f"close of {position['id']} failed: HTTP {status} {str(data)[:180]}")
    if reason in ("day-stop", "weekend", "time-exit"):
        await _notify_http(
            title=f"eToro agent: position closed ({reason})",
            message=(f"Position {position['id']} on instrument {position['instrument_id']} "
                     f"closed by the {reason} guardrail."),
            priority="high" if reason == "day-stop" else "default",
            tags="warning" if reason == "day-stop" else "no_entry_sign",
        )
    return OrderResult(ok=True, position_id=position["id"], detail=f"closed ({reason})")


@workflows.activity(start_to_close_timeout=TIMEOUT)
async def manage_positions(
    config: SessionConfig, state: dict[str, Any], now_ctx: dict[str, Any]
) -> dict[str, Any]:
    """Day stop, weekend flat, time exit: {closed, events, halted}. Never raises."""
    events: list[str] = []
    positions = state.get("positions") or []
    halted = False
    if not positions:
        return {"closed": [], "events": [], "halted": False}

    async def close_all(reason: str) -> list[int]:
        nonlocal halted
        closed = []
        for p in positions:
            res = await close_position(config, p, reason)
            closed.append(p["id"])
            events.append(f"{reason} close {p['id']}: {'ok' if res.ok else res.detail}")
            halted = halted or (not res.ok and res.halt_session)
        return closed

    if rules.day_stop_hit(state, config):
        closed = await close_all("day-stop")
        await _notify_http(
            title="eToro agent: daily stop",
            message=(f"Day P/L {state.get('today_net_pnl')} reached the stop: "
                     f"all positions closed, no more entries today."),
            priority="high", tags="warning",
        )
        return {"closed": closed, "events": events, "halted": True}

    if rules.weekend_flat_due(now_ctx, config):
        closed = await close_all("weekend")
        return {"closed": closed, "events": events, "halted": halted}

    for p in rules.time_exit_positions(positions, now_ctx, config):
        res = await close_position(config, p, "time-exit")
        events.append(f"time-exit close {p['id']}: {'ok' if res.ok else res.detail}")
        halted = halted or (not res.ok and res.halt_session)
    # All closed ids from the time exit (computed twice: determinism preserves order).
    closed = [p["id"] for p in rules.time_exit_positions(positions, now_ctx, config)]
    return {"closed": closed, "events": events, "halted": halted}


# ------------------------------------------------------------------
# notify (ntfy)
# ------------------------------------------------------------------
def _header_value(value: str) -> str:
    """httpx refuses non-ASCII header values: encode them as %XX UTF-8 (ntfy native)."""
    if value.isascii():
        return value
    return "".join(
        chr(b) if b < 0x80 else f"%{b:02X}" for b in value.encode("utf-8")
    )


async def _notify_http(title: str, message: str, priority: str = "default", tags: str = "") -> dict[str, Any]:
    """Plain helper (activities never call other activities): publish to ntfy."""
    topic = etoro_env.ntfy_topic
    if not topic:
        logger.warning("ntfy topic absent: notification skipped", title=title)
        return {"delivered": False, "reason": "no topic"}
    base = etoro_env.ntfy_base_url.rstrip("/")
    url = f"{base}/{topic}"
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(
            url,
            content=message.encode("utf-8"),
            headers={
                "Title": _header_value(title),
                "Priority": priority,
                "Tags": tags,
                "User-Agent": "etoro-agent-workflow/2.0",
            },
        )
    return {"delivered": res.status_code == 200, "http_status": res.status_code}


@workflows.activity(start_to_close_timeout=TIMEOUT, retry_policy_max_attempts=1)
async def notify(title: str, message: str, priority: str = "default", tags: str = "") -> dict[str, Any]:
    """Publish to ntfy. Missing topic logs a warning and returns not-delivered."""
    return await _notify_http(title, message, priority, tags)
