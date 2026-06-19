"""Structured result reporting for the webcompat-triage agent."""

from __future__ import annotations

from claude_agent_sdk import McpServerConfig, create_sdk_mcp_server, tool
from pydantic import BaseModel, Field, ValidationError

RESULT_SERVER_NAME = "webcompat-triage"
SUBMIT_RESULT_TOOL = f"mcp__{RESULT_SERVER_NAME}__submit_result"


class TriageResult(BaseModel):
    """Canonical result the agent produces for a web-compat investigation."""

    reproduced: bool = Field(
        description="Whether the reported issue reproduced in Firefox.",
    )
    chrome_mask_fixed: bool | None = Field(
        description=(
            "Whether enabling the Chrome Mask extension (spoofing a Chrome "
            "User-Agent) fixed the reported behavior: true if it fixed it, "
            "false if it did not, null if the Chrome Mask test was not run "
            "(e.g. the issue did not reproduce at baseline)."
        ),
    )
    summary: str = Field(
        description="Human-readable report of what was observed.",
    )
    steps: str = Field(
        description=(
            "Ordered steps to reproduce the issue at baseline (Chrome Mask off), "
            "as a single numbered list (1., 2., 3., ...), one step per line. Do "
            "not include the Chrome Mask enabling/testing steps."
        ),
    )


SUBMIT_RESULT_SCHEMA = {
    **TriageResult.model_json_schema(),
    "additionalProperties": False,
}


class ResultCollector:
    """Holds the result submitted by the agent, if any."""

    def __init__(self) -> None:
        self.result: TriageResult | None = None


def build_result_server(collector: ResultCollector) -> McpServerConfig:
    """Build an in-process MCP server exposing the ``submit_result`` tool.

    The handler validates the payload against :class:`TriageResult` and stores
    it on ``collector``. A validation error is returned to the model (as tool
    output) so it can correct and resubmit rather than failing the run.
    """

    @tool(
        "submit_result",
        "Submit the final web-compatibility investigation result. Call exactly "
        "once, at the end, after completing the investigation.",
        SUBMIT_RESULT_SCHEMA,
    )
    async def submit_result(args: dict) -> dict:
        try:
            collector.result = TriageResult.model_validate(args)
        except ValidationError as exc:
            return {
                "content": [{"type": "text", "text": f"Invalid result: {exc}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": "Result recorded."}]}

    return create_sdk_mcp_server(name=RESULT_SERVER_NAME, tools=[submit_result])
