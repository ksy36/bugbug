import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from hackbot_runtime import (
    HackbotAgentResult,
    HackbotContext,
    run_async,
)
from hackbot_runtime.backends import HttpServer
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent import (
    BugDataInput,
    BugIdInput,
    DiagnosisResult,
    TaskConfig,
    run_autowebcompat_diagnosis,
)
from .browser import ChromeBrowsers, FirefoxBrowsers

logger = logging.getLogger("autowebcompat-diagnosis")


class AgentInputs(BaseSettings):
    bugzilla_mcp_url: str
    # Single-bug inputs. Ignored when bugs_file is set (batch mode wins).
    bug_data: str | None = None
    bug_id: int | None = None
    # Batch mode: path to a mounted JSON file with a list of bug objects, e.g.
    # [{"id": 1085010, "summary": "...", "description": "..."}, ...].
    bugs_file: str | None = None
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


class BatchEntryResult(BaseModel):
    """Outcome of diagnosing one bug in a batch run."""

    bug_id: int | None = None
    status: Literal["succeeded", "failed"]
    backend: str
    model: str | None = None
    num_turns: int = 0
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    result: DiagnosisResult | None = None
    error: str | None = None


class AutowebcompatDiagnosisBatchResult(HackbotAgentResult):
    """Result of a batch run: one entry per bug, plus roll-up totals."""

    backend: str
    model: str | None = None
    entries: list[BatchEntryResult]
    start_time: datetime
    end_time: datetime


def bug_data_from_entry(entry: dict[str, Any]) -> BugDataInput:
    """Build a diagnosis input from a bugs-file entry (id/summary/description)."""
    parts: list[str] = []
    if entry.get("summary"):
        parts.append(f"Summary: {entry['summary']}")
    if entry.get("description"):
        parts.append(str(entry["description"]))
    if not parts:
        raise ValueError(f"entry has no summary/description: {entry!r}")
    raw_id = entry.get("id")
    return BugDataInput(bug_data="\n\n".join(parts), bug_id=int(raw_id) if raw_id else None)


def task_config(inputs: AgentInputs, ctx: HackbotContext) -> TaskConfig:
    return TaskConfig(
        model=inputs.model,
        max_turns=inputs.max_turns,
        effort=inputs.effort,
        log=ctx.log_path,
        verbose=True,
        backend=inputs.backend,
    )


async def run_single(ctx: HackbotContext) -> AutowebcompatDiagnosisResult:
    start_time = datetime.now()
    inputs = AgentInputs()  # type: ignore

    if inputs.bug_data is not None:
        input_data: BugDataInput | BugIdInput = BugDataInput(bug_data=inputs.bug_data)
    elif inputs.bug_id is not None:
        input_data = BugIdInput(bug_id=inputs.bug_id)
    else:
        raise ValueError("provide at least one of bug_data or bug_id")

    result, stats = await run_autowebcompat_diagnosis(
        task_config(inputs, ctx),
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


# Written after every bug so a crash mid-batch never loses completed work, and
# read back on restart (same RUN_ID) to resume. Lives at
# run_artifacts_dir / BATCH_PROGRESS_KEY.
BATCH_PROGRESS_KEY = "batch_progress.json"


def build_batch_result(
    inputs: AgentInputs,
    entries: list[BatchEntryResult],
    start_time: datetime,
    end_time: datetime,
) -> AutowebcompatDiagnosisBatchResult:
    return AutowebcompatDiagnosisBatchResult(
        backend=inputs.backend,
        model=inputs.model,
        entries=entries,
        num_turns=sum(e.num_turns for e in entries),
        total_cost_usd=(
            sum(e.total_cost_usd for e in entries if e.total_cost_usd is not None)
            or None
        ),
        start_time=start_time,
        end_time=end_time,
    )


def load_checkpoint(ctx: HackbotContext) -> list[BatchEntryResult]:
    """Load previously-completed entries for this RUN_ID, if any."""
    path = ctx.run_artifacts_dir / BATCH_PROGRESS_KEY
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        entries = [BatchEntryResult(**e) for e in data.get("entries", [])]
        logger.info("Resuming: %d bug(s) already done in %s", len(entries), path)
        return entries
    except Exception:
        logger.warning("Could not read checkpoint %s; starting fresh", path)
        return []


async def run_batch(ctx: HackbotContext) -> AutowebcompatDiagnosisBatchResult:
    start_time = datetime.now()
    inputs = AgentInputs()  # type: ignore

    bugs = json.loads(Path(inputs.bugs_file).read_text())  # type: ignore[arg-type]
    if not isinstance(bugs, list):
        raise ValueError("bugs_file must contain a JSON list of bug objects")
    logger.info("Batch: %d bug(s) on backend=%s", len(bugs), inputs.backend)

    # Install the browsers once for the whole batch (a fresh per-bug download
    # would repeat this for every bug). Reused across all diagnoses below.
    logger.info("Installing Firefox + Chrome for the batch...")
    firefox_path = FirefoxBrowsers().stable
    chrome_path = ChromeBrowsers().stable

    # Resume: carry over prior entries, but only SKIP bugs that already
    # succeeded — failed ones are retried (a failure may be a transient
    # timeout). Drop prior failed entries so the retry replaces them.
    prior = load_checkpoint(ctx)
    done_ids = {e.bug_id for e in prior if e.status == "succeeded" and e.bug_id}
    entries: list[BatchEntryResult] = [
        e for e in prior if e.status == "succeeded"
    ]

    def checkpoint() -> None:
        # Overwrite the progress file with everything done so far.
        payload = build_batch_result(
            inputs, entries, start_time, datetime.now()
        ).model_dump(mode="json")
        ctx.publish_json(BATCH_PROGRESS_KEY, payload)

    for i, entry in enumerate(bugs, start=1):
        try:
            input_data = bug_data_from_entry(entry)
        except ValueError as exc:
            logger.warning("Skipping entry %d: %s", i, exc)
            entries.append(
                BatchEntryResult(
                    status="failed", backend=inputs.backend, model=inputs.model,
                    error=str(exc),
                )
            )
            checkpoint()
            continue

        if input_data.bug_id is not None and input_data.bug_id in done_ids:
            logger.info(
                "Batch %d/%d: bug %s already done, skipping",
                i, len(bugs), input_data.bug_id,
            )
            continue

        logger.info("Batch %d/%d: %s", i, len(bugs), input_data.subject())
        try:
            result, stats = await run_autowebcompat_diagnosis(
                task_config(inputs, ctx),
                input_data,
                bugzilla_mcp_server=HttpServer(url=inputs.bugzilla_mcp_url),
                publish_file=ctx.publish_file,
                firefox_path=firefox_path,
                chrome_path=chrome_path,
            )
            entries.append(
                BatchEntryResult(
                    bug_id=input_data.bug_id,
                    status="succeeded",
                    backend=inputs.backend,
                    model=inputs.model,
                    num_turns=stats.num_turns,
                    total_cost_usd=stats.total_cost_usd,
                    usage=stats.usage,
                    result=result,
                )
            )
            # Only successes count as done (failures are retried on resume).
            if input_data.bug_id is not None:
                done_ids.add(input_data.bug_id)
        except Exception as exc:  # keep going: one bad bug shouldn't stop the batch
            logger.exception("Batch %d/%d failed", i, len(bugs))
            entries.append(
                BatchEntryResult(
                    bug_id=input_data.bug_id,
                    status="failed",
                    backend=inputs.backend,
                    model=inputs.model,
                    error=str(exc),
                )
            )
        # Persist after every bug so a crash never loses completed work.
        checkpoint()

    outcome = build_batch_result(inputs, entries, start_time, datetime.now())
    ok = sum(1 for e in entries if e.status == "succeeded")
    logger.info("Batch complete: %d/%d succeeded", ok, len(entries))
    return outcome


async def main(ctx: HackbotContext) -> HackbotAgentResult:
    inputs = AgentInputs()  # type: ignore
    # Treat an empty string (compose sets BUGS_FILE="" when off) as unset.
    if inputs.bugs_file:
        return await run_batch(ctx)
    return await run_single(ctx)


if __name__ == "__main__":
    run_async(main)