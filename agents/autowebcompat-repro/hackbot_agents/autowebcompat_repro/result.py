"""Structured result reporting for the autowebcompat-repro agent.

The reproduction tasks end by calling ``submit_result`` exactly once with a
payload validated against the task's Pydantic result model. That tool works
under both backends off one shared validation path:

  - Claude: the tool runs in-process, so the validated result is stored
    directly on the :class:`ResultCollector` the parent holds.
  - Codex: the tool runs in a standalone stdio MCP server that Codex spawns as
    a child process, so it writes the validated JSON to a result path passed in
    via the environment; the parent reads that file back once the session ends.
"""

from __future__ import annotations

import imghdr
import json
import os
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

RESULT_SERVER_NAME = "autowebcompat-repro"
SUBMIT_RESULT_TOOL = f"mcp__{RESULT_SERVER_NAME}__submit_result"

SUBMIT_RESULT_DESCRIPTION = (
    "Submit the final web-compatibility investigation result. Call exactly "
    "once, at the end, after completing the investigation."
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


class TestPlanResult(BaseModel):
    is_webcompat: bool = Field(
        description=("true if the input describes a webcompat issue, otherwise false."),
    )

    affects_platforms: list[
        Literal["ios"] | Literal["android"] | Literal["desktop"]
    ] = Field(description="List of platforms which seem to be affected by the issue")

    affects_os: (
        None
        | Literal["all"]
        | list[Literal["windows"] | Literal["linux"] | Literal["macos"]]
    ) = Field(
        description="""List of desktop issues known to be affected.
        - `null` if the issue does not affect desktop.
        - "all" if there is no strong evidence that the issue is platform specific"
        - Otherwise a list of platform names which are likely affected
        """
    )

    affects_channels: list[Literal["nightly"] | Literal["stable"] | Literal["esr"]] = (
        Field(
            description="""List of channels affected
        - "esr" if the issue is reported as specific to ESR builds.
        - "stable" if the issue is reported as reproducing on stable builds, or there is no evidence for which channels are affected
        - "nightly" if the issue is reported as reproducing on nightly builds, or there is no evidence for which channels are affected
        """
        )
    )


class ReproductionResult(BaseModel):
    reproduced: bool = Field(
        description=(
            "true if the reported issue reproduced in Firefox, otherwise false."
        ),
    )

    failure_reason: (
        Literal["not_reproducable"]
        | Literal["non_compat"]
        | Literal["unsupported_platform"]
        | Literal["blocked"]
        | Literal["blocked_captcha"]
        | Literal["blocked_geo"]
        | Literal["login"]
        | Literal["down"]
        | Literal["other"]
        | None
    ) = Field(
        description="""If an issue was reproduced as a Firefox web-compat issue then `null`.
        Otherwise, one of the following categories describing the reason for the failure:
          * not_reproducable - When it was possible to run all the steps to reproduce, but no issue was found
          * non_compat - When the issue is not a Firefox web-compat issue. This covers reports that don't refer
          to site breakage (e.g. issues with the Firefox UI or product features such as reader mode) and reports
          whose behavior reproduces identically in both Firefox and Chrome.
          * unsupported_platform - When the report is specific to a platform that isn't available e.g. iOS
          * blocked_captcha - When access to the site was blocked because the page requires solving a captcha
          * blocked_geo - When access to the site was blocked based on location ("geoblocking")
          * blocked - When access to the site was blocked for some reason that couldn't be identified as a captcha or geoblocking
          * login - When reproducing the issue requires completing a login flow
          * down - When the site down or unavailable in a way that is unrelated to the issue report
          * other - When the issue could not be reproduced for some other reason (please give details in the summary text)
"""
    )

    screenshot_path: Path | None = Field(
        description=(
            """The file path you saved a screenshot to via the `screenshot_page`
            `saveTo` parameter, showing the issue. Use the exact path you passed
            as `saveTo` (do NOT paste image data). This must only be set for
            issues where the breakage is visual in nature i.e. incorrect site
            layout rather than broken interaction. Otherwise it must be null."""
        ),
    )

    @field_validator("screenshot_path", mode="after")
    @classmethod
    def validate_screenshot_path(cls, path: Path | None) -> Path | None:
        if path is None:
            return None

        if not path.exists():
            raise ValueError(f"Screenshot path {path} doesn't exist")
        if imghdr.what(str(path)) != "png":
            raise ValueError(f"Screenshot path {path} is not a valid PNG image")
        return path


class BugReproductionResult(ReproductionResult):
    """Canonical result the agent produces for a web-compat investigation.

    Produced by the initial reproduction task, which drives both Firefox and
    Chrome so it can cross-check the two browsers in a single context.
    """

    summary: str = Field(
        description="""A concise account of whether the issue represents a real
        webcompat issue i.e. it can be reproduced in Firefox."""
    )

    chrome_reproduced: bool | None = Field(
        description=(
            "Result of running the cross-check step in Chrome: "
            "true if the issue also reproduces in Chrome, false if the "
            "issue does not reproduce in Chrome, or null if the Chrome "
            "cross-check wasn't able to confirm reproduction."
        ),
    )

    steps: str = Field(
        description=(
            "The ordered steps you took, as a single numbered list (1., 2., 3., "
            "... one step per line), written so another agent could reproduce "
            "them with no extra context. Each step must be self-contained: "
            "whenever you introduce an input or artifact the report did not "
            "provide (a file, image, account, or any other test data), state its "
            "exact origin — the URL you fetched it from, the command you ran, or "
            'how you generated it — not just that you "used" or "saved" it. A '
            "reader must be able to obtain the same inputs. Omit the Chrome cross-check "
            "reproduction and screenshot steps."
        ),
    )


class ChromeMaskResult(BaseModel):
    chrome_mask_fixed: bool | None = Field(
        description=(
            "Whether enabling the Chrome Mask extension (spoofing a Chrome "
            "User-Agent) fixed the reported behavior: true if it fixed it, "
            "false if it did not, null if the Chrome Mask test was not run "
            "(e.g. the issue did not reproduce at baseline)."
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
        args=["-m", "hackbot_agents.autowebcompat_repro.result"],
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
