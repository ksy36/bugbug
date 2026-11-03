"""Snapshot a batch of bugs: ``BUG_IDS="123 456" python -m ...``.

Bugs run one after another. ``<artifacts>/snapshots/index.json`` is rewritten
after every bug, and bugs already marked ``ok`` there are skipped, so an
interrupted batch can be re-run with the same ids.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Literal

from autowebcompat_tools.environment import Environment
from autowebcompat_tools.task import TaskConfig
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent import snapshot_bug
from .browser import install_chrome, install_firefox

logger = logging.getLogger("autowebcompat-snapshot")


class AgentInputs(BaseSettings):
    bug_ids: str
    bugzilla_api_url: str = "https://bugzilla.mozilla.org/rest"
    artifacts_dir: Path = Path("/artifacts")
    model: str | None = None
    max_turns: int | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    headless: bool = False

    model_config = SettingsConfigDict(extra="ignore")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    inputs = AgentInputs()  # type: ignore
    bug_ids = [int(b) for b in re.split(r"[\s,]+", inputs.bug_ids.strip())]

    out_dir = inputs.artifacts_dir / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    with Environment() as env:
        if not inputs.headless:
            env.start_xvfb()
        firefox_path = install_firefox("nightly")
        chrome_path = install_chrome()

        for bug_id in bug_ids:
            if index.get(str(bug_id), {}).get("status") == "ok":
                logger.info("bug %s: already snapshotted, skipping", bug_id)
                continue

            bug_dir = out_dir / str(bug_id)
            bug_dir.mkdir(exist_ok=True)
            config = TaskConfig(
                model=inputs.model,
                max_turns=inputs.max_turns,
                effort=inputs.effort,
                log=bug_dir / "agent.log",
                headless=inputs.headless,
            )
            try:
                entry = await snapshot_bug(
                    config,
                    inputs.bugzilla_api_url,
                    bug_id,
                    firefox_path,
                    chrome_path,
                    bug_dir,
                )
            except Exception as exc:
                logger.exception("bug %s failed", bug_id)
                entry = {"status": "error", "reason": str(exc)}

            logger.info("bug %s: %s", bug_id, entry)
            index[str(bug_id)] = entry
            index_path.write_text(json.dumps(index, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
