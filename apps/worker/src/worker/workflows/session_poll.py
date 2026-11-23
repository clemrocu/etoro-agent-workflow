"""Session poll: one bounded run per M5 close, stateless, API as source of truth.

The Vibe layer creates the bounded schedule (interval PT5M + end_at) that fires
this workflow. Each run: read the clock and the account, manage stops, evaluate
one entry, report. Nothing is kept between runs, so any run is safely retryable
and no Temporal state survives a session.
"""

from typing import Any

from mistralai import workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

with workflow.unsafe.imports_passed_through():
    from worker.activities import (
        evaluate,
        manage_positions,
        now_context,
        place_entry,
        read_account_state,
        read_market,
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
        "One 5-minute polling step of an automated trading session on the eToro "
        "agent portfolio: manage soft stops, forced close and day stop, then evaluate "
        "at most one entry under the project guardrails. Stateless by design."
    ),
)
class SessionPoll:
    @workflows.workflow.entrypoint
    async def run(self, config: SessionConfig | None = None) -> PollResult:
        config = config or SessionConfig()
        now = await now_context()
        state = await read_account_state(config)
        market = await read_market(config)

        managed = await manage_positions(config, state, market, now)
        events = list(managed["events"])

        decision = await evaluate(config, state, market, now)
        if decision["action"] == "open" and not config.dry_run and not managed["halted"]:
            order = await place_entry(config, decision)
            events.append(
                f"entry {decision['direction']}: {'ok' if order.ok else order.detail}"
            )
            status = "entered" if order.ok else "halted"
        elif decision["action"] == "open":
            events.append(f"entry skipped: dry run ({decision['reason'] if not decision.get('reason') else decision['direction']})")
            status = "dry-run"
        else:
            status = "ok"
            events.append(decision["reason"])

        return PollResult(
            status=status,
            minute_of_day=now["minute_of_day"],
            decision=decision.get("reason", decision["action"]),
            events=events,
        )
