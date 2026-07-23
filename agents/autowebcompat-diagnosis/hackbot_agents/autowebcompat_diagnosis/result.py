"""Structured result reporting for the autowebcompat-diagnosis agent.

The single diagnosis task ends by calling ``submit_result`` exactly once with a
payload validated against :class:`DiagnosisResult`. That tool works under both
backends off one shared validation path:

  - Claude: the tool runs in-process, so the validated result is stored
    directly on the :class:`ResultCollector` the parent holds.
  - Codex: the tool runs in a standalone stdio MCP server that Codex spawns as
    a child process, so it writes the validated JSON to a result path passed in
    via the environment; the parent reads that file back once the session ends.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, ValidationError

RESULT_SERVER_NAME = "autowebcompat-diagnosis"
SUBMIT_RESULT_TOOL = f"mcp__{RESULT_SERVER_NAME}__submit_result"

SUBMIT_RESULT_DESCRIPTION = (
    "Submit the final web-compatibility diagnosis. Call exactly once, at the "
    "end, after investigating the issue in both Firefox and Chrome."
)

# Env vars the Codex child reads: where to write the validated result, and
# which Pydantic result model to validate the payload against.
RESULT_PATH_ENV = "AUTOWEBCOMPAT_RESULT_PATH"
RESULT_CLS_ENV = "AUTOWEBCOMPAT_RESULT_CLS"

ResultT = TypeVar("ResultT", bound=BaseModel)


class ResultCollector(Generic[ResultT]):
    """Holds the result submitted by the agent, if any."""

    def __init__(self, result_cls: type[ResultT]) -> None:
        self.result_cls: type[ResultT] = result_cls
        self.result: ResultT | None = None


class DiagnosisResult(BaseModel):
    """Root-cause analysis the agent produces for a web-compat issue.

    The diagnosis task drives both Firefox and Chrome in a single context so it
    can compare their behaviour and attribute the difference. This result is
    analysis only: no fix, patch, or recorded action.
    """

    reproduced: bool = Field(
        description=(
            "Whether the reported broken behaviour was observed in Firefox "
            "during the investigation."
        ),
    )

    root_cause: str = Field(
        description=(
            "The root-cause hypothesis: what concretely differs between Firefox "
            "and Chrome that produces the reported behaviour. Reference the "
            "specific evidence you gathered (console errors, failed network "
            "requests, differing DOM/JS behaviour, feature detection, "
            "user-agent sniffing, unsupported APIs, etc.). If the cause could "
            "not be determined, say so and explain what was ruled out."
        ),
    )

    firefox_findings: str = Field(
        description=(
            "What you observed in Firefox: the concrete symptoms, and the "
            "console / network / DOM evidence behind them."
        ),
    )

    chrome_findings: str = Field(
        description=(
            "What you observed in Chrome running the same steps: whether the "
            "behaviour differs, and the evidence for the difference."
        ),
    )

    difference: str = Field(
        description=(
            "A direct comparison of Firefox vs Chrome for this issue — what "
            "worked in one and not the other, and the observable divergence "
            "that points at the root cause."
        ),
    )

    confidence: str = Field(
        description=(
            "How confident you are in the root-cause hypothesis: one of "
            "'high', 'medium', or 'low', with a one-line justification."
        ),
    )

    steps: str = Field(
        description=(
            "The ordered steps you took, as a single numbered list (1., 2., 3., "
            "... one step per line), written so another agent could reproduce "
            "the investigation with no extra context. Each step must be "
            "self-contained: whenever you introduce an input or artifact the "
            "report did not provide, state its exact origin (a URL, a command, "
            "how you generated it) — not just that you used it."
        ),
    )

    testcase_created: bool = Field(
        description=(
            "Whether you wrote a minimal reduced test case to the path given in "
            "the task details and confirmed it reproduces the Firefox/Chrome "
            "difference. Set false if you could not produce a minimal repro."
        ),
    )

    # Populated by the runtime after the run from the published test-case
    # artifact — not something the agent submits. Kept out of the tool's
    # required inputs via its default.
    testcase_url: str | None = Field(
        default=None,
        description=(
            "Leave unset. The runtime fills this with the URL of the reduced "
            "test case it published."
        ),
    )


def submit_result_schema(result_cls: type[BaseModel]) -> dict[str, Any]:
    """The JSON Schema the submit_result tool accepts for ``result_cls``."""
    return {**result_cls.model_json_schema(), "additionalProperties": False}


def validate_payload(
    result_cls: type[BaseModel], args: dict[str, Any]
) -> tuple[BaseModel | None, str | None]:
    """Validate a submit_result payload.

    Returns ``(result, None)`` on success or ``(None, error_text)`` on failure.
    The error text is fed back to the model as tool output so it can correct and
    resubmit rather than failing the run. Shared by both backends so the accepted
    payload and error feedback are identical regardless of engine.
    """
    try:
        return result_cls.model_validate(args), None
    except ValidationError as exc:
        return None, f"Invalid result: {exc}"


def build_result_server(collector: ResultCollector):
    """Build the in-process Claude SDK MCP server exposing ``submit_result``.

    The handler validates the payload and stores it on ``collector``. A
    validation error is returned to the model (as tool output) so it can correct
    and resubmit rather than failing the run.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "submit_result",
        SUBMIT_RESULT_DESCRIPTION,
        submit_result_schema(collector.result_cls),
    )
    async def submit_result(args: dict) -> dict:
        result, error = validate_payload(collector.result_cls, args)
        if error is not None:
            return {"content": [{"type": "text", "text": error}], "is_error": True}
        collector.result = result
        return {"content": [{"type": "text", "text": "Result recorded."}]}

    return create_sdk_mcp_server(name=RESULT_SERVER_NAME, tools=[submit_result])


def build_codex_result_server(result_cls: type[BaseModel], result_path: Path):
    """Describe the stdio server Codex spawns to serve ``submit_result``.

    The child runs this module with ``RESULT_PATH_ENV`` pointing at
    ``result_path`` and ``RESULT_CLS_ENV`` naming the result model; on submit it
    writes the validated JSON there, which the parent reads once the session
    ends. Returns a neutral :class:`~hackbot_runtime.backends.StdioServer`.
    """
    from hackbot_runtime.backends import StdioServer

    ref = f"{result_cls.__module__}:{result_cls.__qualname__}"
    return StdioServer(
        command="python",
        args=["-m", "hackbot_agents.autowebcompat_diagnosis.result"],
        env={RESULT_PATH_ENV: str(result_path), RESULT_CLS_ENV: ref},
    )


def read_codex_result(result_cls: type[ResultT], result_path: Path) -> ResultT | None:
    """Read back the result the Codex child wrote, if any."""
    if not result_path.exists():
        return None
    result, _ = validate_payload(result_cls, json.loads(result_path.read_text()))
    return result


async def serve_stdio_result(result_cls: type[BaseModel], result_path: Path) -> None:
    """Serve ``submit_result`` as a standalone stdio MCP server (Codex child).

    stdout is the MCP protocol channel; nothing human-readable may go there.
    """
    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server(RESULT_SERVER_NAME, version="0.1.0")

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="submit_result",
                description=SUBMIT_RESULT_DESCRIPTION,
                inputSchema=submit_result_schema(result_cls),
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> mcp_types.CallToolResult:
        if name != "submit_result":
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=f"unknown tool: {name}")
                ],
                isError=True,
            )
        result, error = validate_payload(result_cls, arguments or {})
        if error is not None:
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=error)],
                isError=True,
            )
        # Marshal the validated result back to the parent across the process
        # boundary. Write atomically so a partial file is never read.
        tmp = result_path.with_suffix(result_path.suffix + ".tmp")
        tmp.write_text(result.model_dump_json())
        tmp.replace(result_path)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="Result recorded.")]
        )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main() -> None:
    """Entry point for the Codex-spawned ``submit_result`` child process."""
    import asyncio
    import importlib

    result_path = Path(os.environ[RESULT_PATH_ENV])
    module_name, _, cls_name = os.environ[RESULT_CLS_ENV].partition(":")
    result_cls = getattr(importlib.import_module(module_name), cls_name)
    asyncio.run(serve_stdio_result(result_cls, result_path))


if __name__ == "__main__":
    main()
