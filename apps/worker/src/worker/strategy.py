"""The strategy seam of the socle: where a future trading strategy plugs in.

The socle is strategy-first-neutral: `decide` is the single entry point a future
strategy replaces. Its contract is deliberately narrow and stateless: it receives
the market snapshot and the account state and returns a Decision. Guardrails
(window, day stop, limits, sizing) live in `worker.rules` and run regardless of
the strategy, so no strategy can bypass a project guardrail.
"""

from typing import Literal

from pydantic import BaseModel

from worker.config import SessionConfig


class Decision(BaseModel):
    action: Literal["open", "skip"] = "skip"
    direction: Literal["buy", "sell"] | None = None
    reason: str = "strategy stub: no strategy plugged yet"


def decide(market: dict, state: dict, config: SessionConfig) -> Decision:
    """Stub v0: never opens a position. Replace the body to plug a strategy.

    `market`: {ask, bid, spread_pips, m5: [...closed candles], m15: [...]}
    `state`:  {balance, positions: [...], today_trades, today_net_pnl, ...}
    """
    return Decision(action="skip", reason="strategy stub: no strategy plugged yet")
