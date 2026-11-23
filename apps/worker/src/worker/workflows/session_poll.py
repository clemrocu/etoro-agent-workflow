"""Session poll: one bounded run per M5 close, stateless, API as source of truth.

The Vibe layer creates the bounded schedule (interval PT5M + end_at) that fires
this workflow. Each run: read the clock, the account and the whole market
universe, manage exits (day stop, weekend flat, time exit), evaluate at most one
pullback entry under the project guardrails, report. Nothing is kept between
runs, so any run is safely retryable and no Temporal state survives a session.
"""

from typing import Any

from mistralai import workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

with workflow.unsafe.imports_passedthrough():
    from worker.activities import (
        evaluate,
        manage_positions,
        now_context,
        place_entry,
        read_account_state,
        read_markets,
    )
    from worker.config import SessionConfig


class PollResult(BaseModel):
    status: str
    minute_of_day: int
    decision: str
    events: list[str] = []


@workflows.workflow.define(
    name="session-poll",
    workflow_display_name="eToro agent: session poll",
    workflow_description=(
        "One 5-minute polling step of the pullback swing strategy on the eToro "
        "agent portfolio: manage exits (day stop, weekend flat, time exit), then "
        "evaluate at most one entry across the multi-session universe under the "
        "project guardrails. Stateless by design."
    ),
)
class SessionPoll:
    @workflows.workflow.entrypoint
    async def run(self, config: SessionConfig | None = None) -> PollResult:
        config = config or SessionConfig()
        now = await now_context()
        state = await read_account_state(config)
        markets = await read_markets(config)

        managed = await manage_positions(config, state, now)
        events = list(managed["events"])

        # A managed exit failure halts the session: skip the entry.
        if state.get("read_error"):
            events.append(f"account state: {state['read_error']}")
            return PollResult(
                status="halted", minute_of_day=now["minute_of_day"],
                decision="skip", events=events,
            )

        # Positions the exit pass may have closed are no longer candidates.
        closed_ids = set(managed.get("closed") or [])
        if closed_ids:
            state["positions"] = [
                p for p in state.get("positions") or [] if p["id"] not in closed_ids
            ]

        decision = await evaluate(config, state, markets, now)
        if decision["action"] == "open":
            if config.dry_run:
                events.append(
                    f"dry-run entry skipped: {decision['symbol']} "
                    f"{decision['direction']} @ {decision['entry_rate']:.5f} "
                    f"SL {decision['order_spec']['StopLossRate']} "
                    f"TP {decision['order_spec']['TakeProfitRate']}"
                )
                status = "dry-run"
            elif managed["halted"]:
                events.append("entry skipped: session halted by the exit pass")
                status = "halted"
            else:
                order = await place_entry(config, decision)
                events.append(
                    f"entry {decision['symbol']} {decision['direction']}: "
                    f"{'ok' if order.ok else order.detail}"
                )
                status = "entered" if order.ok else "halted"
        else:
            status = "ok"
            events.append(decision["reason"])

        return PollResult(
            status=status,
            minute_of_day=now["minute_of_day"],
            decision=(
                f"{decision.get('symbol')} {decision.get('direction')}"
                if decision["action"] == "open" else decision.get("reason", "skip")
            ),
            events=events,
        )
