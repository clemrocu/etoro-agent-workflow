"""Socle activities: clock, account state, market snapshot, decision, execution, notify.

All non-deterministic or side-effecting work lives here (Temporal sandbox): the
workflow bodies only orchestrate. Order-sending activities are single-attempt
(retry_policy_max_attempts=1) and own their idempotent retry, so Temporal replay
can never double-fire an order. Activities never log credentials.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote

import httpx
import structlog
from mistralai import workflows
from pydantic import BaseModel

from env.etoro import env as etoro_env
from worker import etoro as etoro_lib
from worker import rules, strategy
from worker.config import SessionConfig

TIMEOUT = timedelta(seconds=90)
logger = structlog.get_logger("etoro-agent")

PARIS = etoro_lib.PARIS
PIP = etoro_lib.PIP


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
    positions = await client.get_positions(config.instrument_id)
    trades = await client.get_today_trades(config.instrument_id)
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
async def read_market(config: SessionConfig) -> dict[str, Any]:
    """Live rate + closed M5/M15 candles for the instrument."""
    if not etoro_env.has_auth():
        return {"read_error": "no eToro credential configured"}
    client = etoro_lib.EtoroClient()
    rate = await client.get_rate(config.instrument_id)
    if rate is None:
        return {"read_error": "rate unreadable"}
    ask, bid = rate
    m5 = await client.get_candles(config.instrument_id, "FiveMinutes", 60, etoro_lib.M5)
    m15 = await client.get_candles(config.instrument_id, "FifteenMinutes", 60, etoro_lib.M15)
    return {
        "ask": ask,
        "bid": bid,
        "spread_pips": round((ask - bid) / PIP, 2),
        "m5": m5 or [],
        "m15": m15 or [],
    }


# ------------------------------------------------------------------
# decision (strategy + guardrails)
# ------------------------------------------------------------------
@workflows.activity(start_to_close_timeout=TIMEOUT)
async def evaluate(
    config: SessionConfig, state: dict[str, Any], market: dict[str, Any], now_ctx: dict[str, Any]
) -> dict[str, Any]:
    """Strategy decision under the guardrails: {action: open|skip, reason, order_spec?}."""
    blocked = rules.entry_blocked_reason(state, now_ctx, config)
    if blocked:
        return {"action": "skip", "reason": f"guardrail: {blocked}"}
    if market.get("read_error"):
        return {"action": "skip", "reason": f"guardrail: {market['read_error']}"}
    if not rules.spread_ok(market["ask"], market["bid"], config):
        return {"action": "skip", "reason": f"guardrail: spread {market['spread_pips']} pips too wide"}
    decision = strategy.decide(market, state, config)
    if decision.action == "skip":
        return {"action": "skip", "reason": decision.reason}
    direction = decision.direction or "buy"
    entry_rate = market["ask"] if direction == "buy" else market["bid"]
    sizing, reason = rules.size_trade(state["balance"], entry_rate, config)
    if sizing is None:
        return {"action": "skip", "reason": f"guardrail: {reason}"}
    return {
        "action": "open",
        "direction": direction,
        "entry_rate": entry_rate,
        "sizing": sizing,
        "order_spec": rules.build_order_spec(direction, entry_rate, sizing, config),
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
        found = await _detect_new_position(client, config, set(p["id"] for p in []), deadline=8)
        if found:
            await _notify_http(
                title="eToro agent: order in timeout, position opened",
                message=f"Position {found['id']} visible after HTTP {status}. Check the account.",
                priority="high", tags="warning",
            )
            return OrderResult(ok=True, position_id=found["id"], halt_session=True,
                               detail=f"opened after HTTP {status}")
        return OrderResult(ok=False, halt_session=True,
                           detail=f"order rejected: HTTP {status} {str(data)[:180]}")
    order_id = ((data or {}).get("orderForOpen") or {}).get("orderID")
    found = await _detect_new_position(client, config, set(), deadline=15)
    await _notify_http(
        title="eToro agent: position opened",
        message=(f"{decision['direction'].upper()} {decision['sizing']['units']} units "
                 f"@ ~{decision['entry_rate']:.5f} (order {order_id}). "
                 f"TP/SL set per config."),
        priority="default", tags="chart_with_upwards_trend",
    )
    return OrderResult(ok=True, order_id=order_id, position_id=found["id"] if found else None,
                       detail=f"order {order_id} accepted")


async def _detect_new_position(client, config: SessionConfig, pre_ids: set, deadline: float = 15):
    end = asyncio.get_running_loop().time() + deadline
    while asyncio.get_running_loop().time() < end:
        positions = await client.get_positions(config.instrument_id)
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
    body = {"InstrumentID": config.instrument_id}
    rid = str(uuidlib.uuid4())
    status, data = await client.call("POST", path, body=body, request_id=rid)
    if status in etoro_lib.RETRYABLE:
        await asyncio.sleep(4)
        status, data = await client.call("POST", path, body=body, request_id=rid)
    if status != 200:
        return OrderResult(ok=False, halt_session=True,
                          detail=f"close of {position['id']} failed: HTTP {status} {str(data)[:180]}")
    if reason == "softsl":
        await _notify_http(
            title="eToro agent: soft stop applied",
            message=f"Position {position['id']} closed: price crossed PRU +/- soft SL.",
            priority="high", tags="warning",
        )
    return OrderResult(ok=True, position_id=position["id"], detail=f"closed ({reason})")


@workflows.activity(start_to_close_timeout=TIMEOUT)
async def manage_positions(
    config: SessionConfig, state: dict[str, Any], market: dict[str, Any], now_ctx: dict[str, Any]
) -> dict[str, Any]:
    """Soft stops + forced close + day stop: returns what happened, never raises."""
    events: list[str] = []
    positions = state.get("positions") or []
    halted = False
    if not positions:
        return {"closed": [], "events": [], "halted": False}
    if market.get("read_error"):
        return {"closed": [], "events": ["market unreadable: stops not managed this poll"],
                "halted": False}
    if rules.past_force_close(now_ctx, config):
        for p in positions:
            res = await close_position(config, p, "force")
            events.append(f"forced close {p['id']}: {'ok' if res.ok else res.detail}")
            halted = halted or (not res.ok and res.halt_session)
        return {"closed": [p["id"] for p in positions], "events": events, "halted": halted}
    if rules.day_stop_hit(state, config):
        for p in positions:
            res = await close_position(config, p, "day-stop")
            events.append(f"day-stop close {p['id']}: {'ok' if res.ok else res.detail}")
            halted = halted or (not res.ok and res.halt_session)
        await _notify_http(
            title="eToro agent: daily stop",
            message=f"Day P/L {state.get('today_net_pnl')} reached the stop: positions closed, session over.",
            priority="high", tags="warning",
        )
        return {"closed": [p["id"] for p in positions], "events": events, "halted": True}
    hits = rules.soft_stop_hits(positions, market["ask"], market["bid"], config)
    for p in hits:
        res = await close_position(config, p, "softsl")
        events.append(f"soft stop {p['id']}: {'ok' if res.ok else res.detail}")
        halted = halted or (not res.ok and res.halt_session)
    return {"closed": [p["id"] for p in hits], "events": events, "halted": halted}


# ------------------------------------------------------------------
# notify (ntfy)
# ------------------------------------------------------------------
def _header_value(value: str) -> str:
    """httpx refuses non-ASCII header values: encode them as %XX UTF-8 (ntfy native)."""
    out = []
    for ch in value.encode("utf-8") if not value.isascii() else value.encode("ascii"):
        if isinstance(ch, int):  # bytes iteration
            if ch < 0x80:
                out.append(chr(ch))
            else:
                out.append(f"%{ch:02X}")
        else:
            out.append(ch)
    return "".join(out)


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
                "User-Agent": "etoro-agent-workflow/1.0",
            },
        )
    return {"delivered": res.status_code == 200, "http_status": res.status_code}


@workflows.activity(start_to_close_timeout=TIMEOUT, retry_policy_max_attempts=1)
async def notify(title: str, message: str, priority: str = "default", tags: str = "") -> dict[str, Any]:
    """Publish to ntfy. Missing topic logs a warning and returns not-delivered."""
    return await _notify_http(title, message, priority, tags)
