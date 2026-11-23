"""Standalone notification maillon: publish a message to ntfy on demand."""

from pydantic import BaseModel

from mistralai import workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from worker.activities import notify


class NotifyInput(BaseModel):
    title: str
    message: str
    priority: str = "default"
    tags: str = ""


class NotifyResult(BaseModel):
    delivered: bool
    http_status: int | None = None


@workflows.workflow.define(
    name="ntfy-notify",
    workflow_display_name="ntfy notification",
    workflow_description="Publish a message to the configured ntfy topic.",
)
class NtfyNotify:
    @workflows.workflow.entrypoint
    async def run(self, request: NotifyInput) -> NotifyResult:
        res = await notify(request.title, request.message, request.priority, request.tags)
        return NotifyResult(delivered=res.get("delivered", False), http_status=res.get("http_status"))
