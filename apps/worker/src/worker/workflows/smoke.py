"""Health check: proves the worker serves workflows, without any external call."""

from pydantic import BaseModel

from mistralai import workflows


class SmokeResult(BaseModel):
    status: str = "ok"
    whoami: str = "etoro-agent worker"


@workflows.workflow.define(
    name="smoke",
    workflow_display_name="smoke check",
    workflow_description="Deployment health check with no external call.",
)
class Smoke:
    @workflows.workflow.entrypoint
    async def run(self) -> SmokeResult:
        return SmokeResult()
