"""Firefox web-compatibility diagnosis agent.

Runs a two-task pipeline over a broken-site report, driving BOTH Firefox and
Chrome via their DevTools MCP servers:

  1. Reproduction: reproduce the reported issue in Firefox and Chrome against
     the real site, then write and *run* a Puppeteer script that drives both
     browsers and demonstrates the divergence. Returns the verified script plus
     structured reproduction findings.
  2. Diagnosis: given the script and findings from task 1, investigate the root
     cause (still with live browsers), write a minimal reduced HTML test case,
     and produce a root-cause analysis.

The bug is passed either inline as ``bug_data`` text or as a Bugzilla ``bug_id``
(read via the Bugzilla broker). Analysis only: the agent never proposes a fix.
"""

from __future__ import annotations

import logging
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal

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
    ReproductionResult,
    ResultCollector,
    ResultT,
    build_codex_result_server,
    build_result_server,
    read_codex_result,
)

HERE = Path(__file__).resolve().parent

logger = logging.getLogger("autowebcompat-diagnosis")

# Uploads a local file under a key and returns its URL (see ctx.publish_file).
PublishFile = Callable[[str, Path, str | None], str]

# Where puppeteer + the DevTools MCP servers are installed in the image.
NODE_MODULES = "/app/node/node_modules"


@dataclass
class BugIdInput:
    bug_id: int
    type: Literal["bug_id"] = "bug_id"

    def subject(self) -> str:
        return f"bug {self.bug_id}"

    def slug(self) -> str:
        """Filename-safe identifier for the published artifacts."""
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
    """Filename-safe short name for the configured model (for artifact names)."""
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
    """Aggregate stats across the pipeline's tasks.

    Claude reports dollars; Codex reports token counts. Both accumulate here.
    """

    num_turns: int = 0
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None

    def add(self, result: Result) -> None:
        self.num_turns += result.num_turns
        if result.total_cost_usd is not None:
            self.total_cost_usd = (self.total_cost_usd or 0.0) + result.total_cost_usd
        if result.usage:
            merged = dict(self.usage or {})
            for key, value in result.usage.items():
                merged[key] = merged.get(key, 0) + value
            self.usage = merged


class Task(ABC, Generic[ResultT]):
    """One agent session in the pipeline.

    Subclasses declare their result model and build the user prompt; the base
    handles backend selection, the per-SDK result server, running the session,
    and returning the validated result.
    """

    name: str = "unnamed-task"
    result_server_name: str = RESULT_SERVER_NAME
    submit_result_tool: str = SUBMIT_RESULT_TOOL
    result_cls: type[ResultT]

    def __init__(
        self,
        task_config: TaskConfig,
        input_data: AutoWebcompatInput,
        firefox_path: Path,
        chrome_path: Path,
        bugzilla_mcp_server: HttpServer,
    ):
        self.task_config = task_config
        self.input_data = input_data
        self.firefox_path = firefox_path
        self.chrome_path = chrome_path
        # Write/Edit let the agent author files (puppeteer script, HTML test
        # case); Bash runs the script to verify.
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
        return (HERE / "prompts" / "system.md").read_text().format(
            task_details=self.task_details()
        )

    @abstractmethod
    def task_details(self) -> str: ...

    def report_intro(self) -> str:
        """Shared preamble telling the agent what report it is working on."""
        if isinstance(self.input_data, BugDataInput):
            return (
                "The web-compatibility report is:\n\n"
                f"{self.input_data.bug_data}\n"
            )
        return (
            f"The web-compatibility report is Bugzilla bug "
            f"{self.input_data.bug_id}. Fetch it using the Bugzilla MCP tools "
            "before you begin.\n"
        )

    @abstractmethod
    def user_prompt(self) -> str: ...

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

    async def run(self, stats: RunStats) -> ResultT:
        subject = self.input_data.subject()
        logger.info("[%s] %s", self.name, subject)

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
            reporter.header(f"{self.name}: {subject}")
            backend = self.build_backend(result_path)
            async with backend:
                async for ev in backend.run_session(
                    self.user_prompt(), system_prompt=self.system_prompt()
                ):
                    reporter.event(ev)
                    if isinstance(ev, Result):
                        final = ev

        if final is None:
            raise AgentError(f"{self.name}: agent produced no result event")
        stats.add(final)
        if final.is_error:
            raise AgentError(f"{self.name} failed: {final.error}")

        # Codex marshals the validated result through a file; Claude stores it
        # in-process on the collector.
        if self.task_config.backend == "codex":
            self.result_collector.result = read_codex_result(
                self.result_cls, result_path
            )
        if self.result_collector.result is None:
            raise AgentError(
                f"{self.name}: agent finished without submitting a result via "
                "submit_result"
            )
        return self.result_collector.result


class ReproductionTask(Task[ReproductionResult]):
    """Task 1: reproduce in both browsers + write and run a Puppeteer script."""

    name = "reproduction"
    result_cls = ReproductionResult

    def __init__(self, *args: Any, script_path: Path, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.script_path = script_path

    def task_details(self) -> str:
        return (
            "Reproduce the reported issue in Firefox and Chrome, then capture "
            "the reproduction as a runnable Puppeteer script.\n"
            "1. Identify the affected URL and the described broken behaviour.\n"
            "2. Reproduce it in Firefox against the real reported site (do not "
            "substitute a reduced test case), gathering console / network / DOM "
            "evidence.\n"
            "3. Run the same steps in Chrome and record how the behaviour "
            "differs.\n"
            "4. Write a Puppeteer reproduction script (ES module) with the Write "
            "tool to exactly this path:\n"
            f"   {self.script_path}\n"
            "   It must drive the REAL reported site in BOTH browsers using "
            "Puppeteer's Firefox and Chrome support, launching each via its "
            "executablePath:\n"
            f"     - Firefox: launch({{ browser: 'firefox', executablePath: '{self.firefox_path}', headless: true }})\n"
            f"     - Chrome:  launch({{ browser: 'chrome',  executablePath: '{self.chrome_path}',  headless: true, args: ['--no-sandbox'] }})\n"
            f"   Import puppeteer from {NODE_MODULES} (set NODE_PATH or import by "
            "absolute path). The script must perform the reproduction steps in "
            "each browser, assert the observable divergence, and print a clear "
            "PASS/FAIL per browser plus a final line stating whether the "
            "difference reproduced.\n"
            f"5. RUN it to verify with the Bash tool: `NODE_PATH={NODE_MODULES} "
            f"node {self.script_path}`. Fix and re-run until it executes cleanly "
            "and demonstrates the Firefox/Chrome difference.\n"
            "6. Call submit_result. Set script_verified=true only if the script "
            "ran and demonstrated the difference. Do not propose a fix."
        )

    def user_prompt(self) -> str:
        return (
            f"{self.report_intro()}\n"
            "Reproduce it in both browsers and produce the verified Puppeteer "
            "reproduction script per your task procedure."
        )


class DiagnosisTask(Task[DiagnosisResult]):
    """Task 2: diagnose root cause given the reproduction + script."""

    name = "diagnosis"
    result_cls = DiagnosisResult

    def __init__(
        self,
        *args: Any,
        testcase_path: Path,
        reproduction: ReproductionResult,
        script_path: Path,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.testcase_path = testcase_path
        self.reproduction = reproduction
        self.script_path = script_path

    def task_details(self) -> str:
        return (
            "Diagnose the root cause of the reported issue, using the "
            "reproduction findings and the verified Puppeteer script from the "
            "reproduction step as your starting evidence.\n"
            "1. Read the Puppeteer reproduction script (it drives the real site "
            f"in both browsers):\n   {self.script_path}\n"
            "2. Investigate WHY Firefox differs from Chrome. Use the Firefox and "
            "Chrome DevTools tools to inspect console errors, network requests, "
            "and probe the DOM / feature detection / user-agent handling with "
            "evaluate_script. You may re-run the script "
            f"(`NODE_PATH={NODE_MODULES} node {self.script_path}`) to observe it.\n"
            "3. Write a minimal reduced HTML test case that reproduces the "
            "difference, with the Write tool, to exactly this path:\n"
            f"   {self.testcase_path}\n"
            "   Include an inline explanation of what should happen and how "
            "Firefox differs from Chrome, then load it in both browsers via the "
            "DevTools tools and confirm it reproduces the difference; revise "
            "until it does.\n"
            "4. Form a root-cause hypothesis grounded in that evidence.\n"
            "5. Call submit_result with your diagnosis. Do not propose a fix."
        )

    def user_prompt(self) -> str:
        return (
            f"{self.report_intro()}\n"
            "The reproduction step already confirmed and captured this issue.\n"
            f"Reproduction summary: {self.reproduction.summary}\n"
            f"Firefox behaviour: {self.reproduction.firefox_findings}\n"
            f"Chrome behaviour: {self.reproduction.chrome_findings}\n"
            f"Script verified: {self.reproduction.script_verified}\n\n"
            "Diagnose the root cause per your task procedure."
        )


async def run_autowebcompat_diagnosis(
    config: TaskConfig,
    input_data: AutoWebcompatInput,
    bugzilla_mcp_server: HttpServer,
    publish_file: PublishFile,
) -> tuple[DiagnosisResult, RunStats]:
    """Run the reproduction -> diagnosis pipeline for a web-compat issue.

    Installs Firefox (stable) and Chrome at runtime and drives both through
    their DevTools MCP servers. Task 1 reproduces the issue and produces a
    verified Puppeteer script; task 2 diagnoses the root cause and writes a
    reduced HTML test case. The script and test case are published via
    ``publish_file`` and their URLs recorded on the diagnosis result. Raises
    :class:`AgentError` on failure.
    """
    firefox_browser = FirefoxBrowsers()
    chrome_browser = ChromeBrowsers()
    firefox_path = firefox_browser.stable
    chrome_path = chrome_browser.stable

    artifacts_dir = Path(tempfile.mkdtemp(prefix="autowebcompat-diagnosis-"))
    stem = f"{input_data.slug()}-{model_slug(config.model)}"
    script_name = f"{stem}.repro.mjs"
    script_path = artifacts_dir / script_name
    testcase_name = f"{stem}.html"
    testcase_path = artifacts_dir / testcase_name

    stats = RunStats()

    # Task 1: reproduce + write/verify the puppeteer script.
    repro_task = ReproductionTask(
        config,
        input_data,
        firefox_path,
        chrome_path,
        bugzilla_mcp_server,
        script_path=script_path,
    )
    reproduction = await repro_task.run(stats)

    script_url = None
    if script_path.exists():
        script_url = publish_file(script_name, script_path, "text/javascript")
    else:
        logger.warning("No puppeteer script written at %s", script_path)

    # If the issue didn't reproduce there is nothing to diagnose: skip task 2
    # and return a result that reflects the failed reproduction.
    if not reproduction.reproduced:
        logger.info("Issue did not reproduce; skipping diagnosis")
        return (
            DiagnosisResult(
                root_cause="Not diagnosed: the issue did not reproduce.",
                firefox_findings=reproduction.firefox_findings,
                chrome_findings=reproduction.chrome_findings,
                difference=reproduction.summary,
                confidence="n/a: not reproduced",
                steps=reproduction.steps,
                testcase_created=False,
                reproduced=False,
                puppeteer_script_url=script_url,
            ),
            stats,
        )

    # Task 2: diagnose using task 1's script + findings.
    diagnosis_task = DiagnosisTask(
        config,
        input_data,
        firefox_path,
        chrome_path,
        bugzilla_mcp_server,
        testcase_path=testcase_path,
        reproduction=reproduction,
        script_path=script_path,
    )
    result = await diagnosis_task.run(stats)

    result.reproduced = reproduction.reproduced
    result.puppeteer_script_url = script_url
    if testcase_path.exists():
        result.testcase_url = publish_file(testcase_name, testcase_path, "text/html")
    else:
        logger.warning("No reduced test case written at %s", testcase_path)

    return result, stats