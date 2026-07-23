"""Firefox web-compatibility diagnosis agent.

Drives a single agent task that reproduces a broken-site report and diagnoses
its root cause by investigating in BOTH Firefox and Chrome via their DevTools
MCP servers and comparing what it observes. The bug is passed either inline as
``bug_data`` text or as a Bugzilla ``bug_id`` (read via the Bugzilla broker).

Analysis only: the agent produces a root-cause explanation, never a fix.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from hackbot_runtime import AgentError
from hackbot_runtime.backends import HttpServer, NeutralServer, Result, load_backend
from hackbot_runtime.claude import Reporter

from .browser import ChromeBrowsers, FirefoxBrowsers
from .config import BUGZILLA_READ_TOOLS, CHROME_DEVTOOLS_TOOLS, DEVTOOLS_TOOLS
from .mcp_servers import build_chrome_devtools_server, build_firefox_devtools_server
from .result import (
    RESULT_SERVER_NAME,
    SUBMIT_RESULT_TOOL,
    DiagnosisResult,
    ResultCollector,
    build_codex_result_server,
    build_result_server,
    read_codex_result,
)

HERE = Path(__file__).resolve().parent

logger = logging.getLogger("autowebcompat-diagnosis")

# Uploads a local file under a key and returns its URL (see ctx.publish_file).
PublishFile = Callable[[str, Path, str | None], str]


@dataclass
class BugIdInput:
    bug_id: int
    type: Literal["bug_id"] = "bug_id"

    def subject(self) -> str:
        return f"bug {self.bug_id}"

    def slug(self) -> str:
        """Filename-safe identifier for the reduced test case."""
        return str(self.bug_id)


@dataclass
class BugDataInput:
    bug_data: str
    type: Literal["bug_data"] = "bug_data"

    def subject(self) -> str:
        return self.bug_data

    def slug(self) -> str:
        return "inline"


AutoWebcompatInput = BugIdInput | BugDataInput


def model_slug(model: str | None) -> str:
    """Filename-safe short name for the configured model (for the testcase name)."""
    if not model:
        return "default"
    base = model.rsplit("/", 1)[-1]
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in base)


@dataclass
class TaskConfig:
    model: str | None = None
    max_turns: int | None = None
    effort: (
        Literal["low"]
        | Literal["medium"]
        | Literal["high"]
        | Literal["xhigh"]
        | Literal["max"]
        | None
    ) = None
    log: Path | None = None
    verbose: bool = True
    # Which agent engine to drive: "claude" (Claude Agent SDK, default) or
    # "codex" (OpenAI Codex SDK).
    backend: Literal["claude", "codex"] = "claude"
    # Directory for per-task scratch files (e.g. the Codex submit_result
    # marshalling file). Defaults to a temp dir when unset.
    result_dir: Path | None = None


@dataclass
class RunStats:
    """Aggregate stats for the single diagnosis session."""

    num_turns: int = 0
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


class DiagnosisTask:
    """Single task: reproduce + diagnose in Firefox and Chrome."""

    name = "diagnosis"
    result_server_name = RESULT_SERVER_NAME
    submit_result_tool = SUBMIT_RESULT_TOOL
    result_cls = DiagnosisResult

    def __init__(
        self,
        task_config: TaskConfig,
        input_data: AutoWebcompatInput,
        firefox_path: Path,
        chrome_path: Path,
        bugzilla_mcp_server: HttpServer,
        testcase_dir: Path,
    ):
        self.task_config = task_config
        self.input_data = input_data
        # Write/Edit let the agent author the reduced test case file.
        self.allowed_tools = [
            "Read",
            "Grep",
            "Glob",
            "Bash",
            "Write",
            "Edit",
            self.submit_result_tool,
        ]
        self.result_collector = ResultCollector(self.result_cls)
        self.stats = RunStats()

        # Absolute path the agent must write its reduced test case to. Named
        # <bugno>-<model>.html so multiple models' outputs don't collide, and
        # published as a run artifact afterward.
        self.testcase_name = (
            f"{input_data.slug()}-{model_slug(task_config.model)}.html"
        )
        self.testcase_path = testcase_dir / self.testcase_name

        # Neutral server descriptors. The backend-specific result server is
        # added at run() time, since Claude serves it in-process while Codex
        # spawns it as a child.
        self.mcp_servers: dict[str, NeutralServer] = {}
        self.add_mcp_server(
            "firefox-devtools",
            build_firefox_devtools_server(
                firefox_path=firefox_path,
                headless=True,
                enable_script=True,
                enable_privileged_context=False,
            ),
            DEVTOOLS_TOOLS,
        )
        self.add_mcp_server(
            "chrome-devtools",
            build_chrome_devtools_server(chrome_path=chrome_path, headless=True),
            CHROME_DEVTOOLS_TOOLS,
        )
        if self.input_data.type == "bug_id":
            self.add_mcp_server("bugzilla", bugzilla_mcp_server, BUGZILLA_READ_TOOLS)

    def add_mcp_server(
        self, name: str, server: NeutralServer, tools: list[str]
    ) -> None:
        self.mcp_servers[name] = server
        self.allowed_tools.extend(tools)

    def system_prompt(self) -> str:
        template = (HERE / "prompts" / "system.md").read_text()
        return template.format(
            task_details=(
                "Reproduce the reported issue in Firefox and diagnose its root "
                "cause by comparing Firefox against Chrome.\n"
                "1. Read the report; identify the affected URL and broken behaviour.\n"
                "2. Reproduce it in Firefox against the real reported site (do not "
                "substitute a reduced testcase), gathering console / network / DOM "
                "evidence.\n"
                "3. Run the same steps in Chrome and record how the behaviour "
                "differs.\n"
                "4. Compare the two browsers to isolate the divergence, then form "
                "a root-cause hypothesis grounded in that evidence.\n"
                "5. Once reproduced, create a minimal reduced test case that "
                "reproduces the difference between the browsers and write it, "
                "using the Write tool, to exactly this path:\n"
                f"   {self.testcase_path}\n"
                "   The test case must include an inline explanation (a comment "
                "or on-page text) of what should happen and how Firefox differs "
                "from Chrome. Then load that file in both Firefox and Chrome via "
                "the DevTools tools and confirm the minimal test case reproduces "
                "the same difference; if it does not, revise it until it does.\n"
                "6. Call submit_result with your diagnosis. Do not propose a fix."
            )
        )

    def user_prompt(self) -> str:
        if isinstance(self.input_data, BugDataInput):
            return (
                "The web-compatibility report to diagnose is:\n\n"
                f"{self.input_data.bug_data}\n\n"
                "Follow your task procedure."
            )
        return (
            f"The web-compatibility report to diagnose is Bugzilla bug "
            f"{self.input_data.bug_id}. Fetch it using the Bugzilla MCP tools, "
            "then follow your task procedure."
        )

    def build_backend(self, result_path: Path):
        """Construct the configured backend, wiring the result server per SDK.

        Claude serves ``submit_result`` in-process; Codex spawns it as a stdio
        child that marshals the validated result back through ``result_path``.
        """
        backend_cls = load_backend(self.task_config.backend)
        if self.task_config.backend == "codex":
            servers: dict[str, NeutralServer] = {
                **self.mcp_servers,
                self.result_server_name: build_codex_result_server(
                    self.result_cls, result_path
                ),
            }
            return backend_cls(
                mcp_servers=servers,
                model=self.task_config.model,
                max_turns=self.task_config.max_turns,
                effort=self.task_config.effort,
                # The codex app-server does not read OPENAI_API_KEY from the
                # environment itself; hand it through so the backend can log in.
                # When unset it falls back to the codex home's existing login.
                api_key=os.environ.get("OPENAI_API_KEY") or None,
            )
        # Claude: the in-process result server is a native SDK config that the
        # ClaudeBackend passes through unchanged alongside neutral servers.
        servers = {
            **self.mcp_servers,
            self.result_server_name: build_result_server(self.result_collector),
        }
        return backend_cls(
            mcp_servers=servers,
            allowed_tools=self.allowed_tools,
            model=self.task_config.model,
            max_turns=self.task_config.max_turns,
            effort=self.task_config.effort,
            # DevTools snapshots of complex pages serialize to JSON that can
            # exceed the SDK's default 1 MiB message buffer (the reader dies
            # fatally if it does). Raise it well above that ceiling.
            max_buffer_size=10 * 1024 * 1024,
        )

    async def run(self, publish_file: PublishFile) -> DiagnosisResult:
        subject = self.input_data.subject()
        logger.info("Diagnosing %s", subject)

        result_dir = self.task_config.result_dir or Path(tempfile.gettempdir())
        fd, result_path_str = tempfile.mkstemp(
            prefix=f"{self.name}-result=", suffix=".json", dir=result_dir
        )
        os.close(fd)
        result_path = Path(result_path_str)

        final: Result | None = None
        with Reporter(
            verbose=self.task_config.verbose, log_path=self.task_config.log
        ) as reporter:
            reporter.header(subject)
            backend = self.build_backend(result_path)
            async with backend:
                async for ev in backend.run_session(
                    self.user_prompt(), system_prompt=self.system_prompt()
                ):
                    reporter.event(ev)
                    if isinstance(ev, Result):
                        final = ev

        if final is None:
            raise AgentError(f"{subject}: agent produced no result event")
        self.stats = RunStats(
            num_turns=final.num_turns,
            total_cost_usd=final.total_cost_usd,
            usage=final.usage,
        )
        if final.is_error:
            raise AgentError(f"{subject} diagnosis failed: {final.error}")

        # Codex marshals the validated result through a file; Claude stores it
        # in-process on the collector.
        if self.task_config.backend == "codex":
            self.result_collector.result = read_codex_result(
                self.result_cls, result_path
            )
        if self.result_collector.result is None:
            raise AgentError(
                f"{subject}: agent finished without submitting a result via "
                "submit_result"
            )

        result = self.result_collector.result
        # Publish the reduced test case if the agent wrote one, and record its
        # URL on the result. Missing file is not fatal — the diagnosis stands on
        # its own even when a minimal reproduction couldn't be produced.
        if self.testcase_path.exists():
            result.testcase_url = publish_file(
                self.testcase_name, self.testcase_path, "text/html"
            )
        else:
            logger.warning(
                "No reduced test case written at %s", self.testcase_path
            )
        return result


async def run_autowebcompat_diagnosis(
    config: TaskConfig,
    input_data: AutoWebcompatInput,
    bugzilla_mcp_server: HttpServer,
    publish_file: PublishFile,
) -> tuple[DiagnosisResult, RunStats]:
    """Diagnose a web-compat issue in Firefox and Chrome.

    Installs Firefox (stable) and Chrome at runtime, drives both through their
    DevTools MCP servers in one agent session, and returns the root-cause
    analysis plus session stats. The agent writes a minimal reduced test case,
    which is published via ``publish_file``. Raises :class:`AgentError` on
    failure.
    """
    firefox_browser = FirefoxBrowsers()
    chrome_browser = ChromeBrowsers()

    testcase_dir = Path(tempfile.mkdtemp(prefix="autowebcompat-testcases-"))

    task = DiagnosisTask(
        config,
        input_data,
        firefox_browser.stable,
        chrome_browser.stable,
        bugzilla_mcp_server,
        testcase_dir,
    )
    result = await task.run(publish_file)
    return result, task.stats
