import logging
from datetime import datetime
from typing import Literal

from hackbot_runtime import (
    HackbotAgentResult,
    HackbotContext,
    run_async,
)
from hackbot_runtime.backends import HttpServer
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent import (
    BugDataInput,
    BugIdInput,
    DiagnosisResult,
    TaskConfig,
    run_autowebcompat_diagnosis,
)

logger = logging.getLogger("autowebcompat-diagnosis")


class AgentInputs(BaseSettings):
    bugzilla_mcp_url: str
    bug_data: str | None = None
    bug_id: int | None = None
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
    backend: Literal["claude", "codex"] = "claude"

    model_config = SettingsConfigDict(extra="ignore")


class AutowebcompatDiagnosisResult(HackbotAgentResult):
    result: DiagnosisResult
    start_time: datetime
    end_time: datetime


async def main(ctx: HackbotContext) -> AutowebcompatDiagnosisResult:
    start_time = datetime.now()
    inputs = AgentInputs()  # type: ignore

    if inputs.bug_data is not None:
        input_data: BugDataInput | BugIdInput = BugDataInput(bug_data=inputs.bug_data)
    elif inputs.bug_id is not None:
        input_data = BugIdInput(bug_id=inputs.bug_id)
    else:
        raise ValueError("provide at least one of bug_data or bug_id")

    result, stats = await run_autowebcompat_diagnosis(
        TaskConfig(
            model=inputs.model,
            max_turns=inputs.max_turns,
            effort=inputs.effort,
            log=ctx.log_path,
            verbose=True,
            backend=inputs.backend,
        ),
        input_data,
        bugzilla_mcp_server=HttpServer(url=inputs.bugzilla_mcp_url),
        publish_file=ctx.publish_file,
    )
    end_time = datetime.now()

    outcome = AutowebcompatDiagnosisResult(
        result=result,
        num_turns=stats.num_turns,
        total_cost_usd=stats.total_cost_usd,
        start_time=start_time,
        end_time=end_time,
    )
    logger.info("Run completed with result: %s", outcome)
    return outcome


if __name__ == "__main__":
    run_async(main)
