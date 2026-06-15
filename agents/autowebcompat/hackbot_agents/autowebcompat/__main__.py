from hackbot_runtime import HackbotContext, run_async
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent import AutoWebcompatResult, run_autowebcompat


class AgentInputs(BaseSettings):
    bugzilla_mcp_url: str
    bug_data: str | None = None
    bug_id: int | None = None
    mode: str = "triage"
    model: str | None = None
    max_turns: int | None = None
    effort: str | None = None
    # Path to the Firefox binary the DevTools MCP should drive. Set in the agent
    # image (FIREFOX_PATH=/opt/firefox/firefox)
    firefox_path: str | None = None

    model_config = SettingsConfigDict(extra="ignore")


async def main(ctx: HackbotContext) -> AutoWebcompatResult:
    inputs = AgentInputs()

    return await run_autowebcompat(
        bugzilla_mcp_server={
            "type": "http",
            "url": inputs.bugzilla_mcp_url,
        },
        mode=inputs.mode,
        bug_data=inputs.bug_data,
        bug_id=inputs.bug_id,
        model=inputs.model,
        max_turns=inputs.max_turns,
        effort=inputs.effort,
        firefox_path=inputs.firefox_path,
        log=ctx.log_path,
        verbose=True,
        actions_recorder=ctx.actions,
    )


if __name__ == "__main__":
    run_async(main)
