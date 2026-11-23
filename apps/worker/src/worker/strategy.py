"""The strategy seam: pullback swing v1 plugs the socle's decision here.

The contract stays deliberately narrow and stateless: `decide` receives one
market snapshot per instrument plus the account state and returns at most one
Decision to execute. Guardrails (window, day stop, limits, sizing) live in
`worker.rules` and run regardless of the strategy, so no strategy can bypass
a project guardrail.
"""

from typing import Literal

from pydantic import BaseModel

from worker import pullback
from worker.config import SessionConfig


class Decision(BaseModel):
    action: Literal["open", "skip"] = "skip"
    instrument_id: int | None = None
    direction: Literal["buy", "sell"] | None = None
    entry_rate: float | None = None
    stop_rate: float | None = None
    tp_rate: float | None = None
    symbol: str | None = None
    reason: str = "no pullback candidate"


def decide(markets: dict[int, dict], state: dict, config: SessionConfig) -> Decision:
    """Best single candidate across the universe, or skip.

    `markets`: {instrument_id: {"ask", "bid", "m15": [...], "h1": [...]}}
    `state`:   {balance, positions: [...], today_trades, today_net_pnl, ...}
    """
    held = {p["instrument_id"] for p in state.get("positions") or []}
    candidates = pullback.decide_markets(markets, held, config.tp_r_multiple)
    if not candidates:
        return Decision(action="skip", reason="no pullback candidate")
    best = candidates[0]
    symbol = best["symbol"]
    held = {p["instrument_id"] for p in state.get("positions") or []}
    # The candidate may be unusable for cost or availability reasons; the
    # cost/spread filter itself lives in evaluate (guardrail). Here the
    # strategy just never proposes an instrument it already holds.
    if best["instrument_id"] in held:
        return Decision(action="skip", reason=f"{symbol} already held")
    return Decision(
        action="open",
        instrument_id=best["instrument_id"],
        direction=best["direction"],
        entry_rate=best["entry"],
        stop_rate=best["stop"],
        tp_rate=best["tp"],
        symbol=symbol,
        reason=best["reason"],
    )
