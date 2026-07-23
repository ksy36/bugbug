"""OpenAI Codex backend.

Drives the agent through the official ``openai-codex`` Python SDK, which talks
JSON-RPC to a local ``codex app-server`` (one per backend, shared by all
sessions in the run). Each ``run_session`` is one fresh thread + one turn.

Differences from the Claude backend, mirroring the split proven in ``larrey``:
  - External tools (browser DevTools, the result server) run as standalone
    stdio MCP servers spawned by Codex; their wiring arrives here as a
    codex-style ``mcp_servers`` config dict.
  - Approval prompts are disabled and the sandbox is left open, mirroring the
    Claude path's ``bypassPermissions``.
  - ``max_turns`` is enforced approximately, by counting the items the agent
    produces and interrupting the turn when the cap is exceeded.
  - Cost reporting is token-based (Codex does not report dollars).

Requires the ``codex-sdk`` optional extra of hackbot-runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, CodexError, Sandbox, models

from .base import (
    AgentBackend,
    AgentEvent,
    AssistantText,
    HttpServer,
    NeutralServer,
    Notice,
    Result,
    SessionStarted,
    Thinking,
    ToolCall,
    ToolResult,
    TurnStart,
)

# hackbot's effort vocabulary -> Codex reasoning effort.
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "max": "xhigh"}

SANDBOX_MAP = {
    "read-only": Sandbox.read_only,
    "workspace-write": Sandbox.workspace_write,
    "full-access": Sandbox.full_access,
}


def to_codex_servers(servers: dict[str, NeutralServer]) -> dict[str, Any]:
    """Convert neutral server descriptors into Codex's mcp_servers config."""
    out: dict[str, Any] = {}
    for name, server in servers.items():
        if isinstance(server, HttpServer):
            entry: dict[str, Any] = {"url": server.url}
            if server.headers:
                entry["headers"] = server.headers
        else:
            entry = {"command": server.command, "args": server.args}
            if server.env:
                entry["env"] = server.env
        out[name] = entry
    return out


def root(item: Any) -> Any:
    """Unwrap the ThreadItem discriminated-union wrapper."""
    return getattr(item, "root", item)


def as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}


def mcp_result_text(item: Any) -> str:
    """Pull the text out of an mcpToolCall result / error."""
    error = getattr(item, "error", None)
    if error is not None and getattr(error, "message", None):
        return error.message
    result = getattr(item, "result", None)
    if result is None:
        return ""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts)


def status_value(obj: Any) -> str | None:
    status = getattr(obj, "status", None)
    return getattr(status, "value", status if status is None else str(status))


def item_started_events(item: Any) -> list[AgentEvent]:
    """Events emitted when an item starts (tool calls show before results)."""
    itype = getattr(item, "type", None)
    if itype == "commandExecution":
        return [ToolCall(name="shell", input={"command": getattr(item, "command", "")})]
    if itype == "mcpToolCall":
        name = f"mcp__{getattr(item, 'server', '?')}__{getattr(item, 'tool', '?')}"
        return [ToolCall(name=name, input=as_dict(getattr(item, "arguments", None)))]
    return []


def item_completed_events(item: Any) -> list[AgentEvent]:
    """Events emitted when an item completes."""
    itype = getattr(item, "type", None)

    if itype == "agentMessage":
        return [AssistantText(text=getattr(item, "text", ""))]

    if itype == "reasoning":
        parts = getattr(item, "summary", None) or getattr(item, "content", None) or []
        text = "\n".join(parts)
        return [Thinking(text=text)] if text else []

    if itype == "commandExecution":
        status = status_value(item)
        return [
            ToolResult(
                text=getattr(item, "aggregated_output", None)
                or f"(exit status: {status})",
                is_error=status not in ("completed", None),
            )
        ]

    if itype == "mcpToolCall":
        status = status_value(item)
        is_error = (
            status not in ("completed", None)
            or getattr(item, "error", None) is not None
        )
        return [ToolResult(text=mcp_result_text(item), is_error=is_error)]

    if itype == "fileChange":
        changes = [
            getattr(change, "path", str(change))
            for change in getattr(item, "changes", None) or []
        ]
        status = status_value(item)
        return [
            ToolCall(name="apply_patch", input={"changes": changes}),
            ToolResult(
                text=f"(patch status: {status})",
                is_error=status not in ("completed", None),
            ),
        ]

    if itype == "webSearch":
        return [
            ToolCall(name="web_search", input={"query": getattr(item, "query", "")})
        ]

    if itype in ("userMessage", None):
        # Our own prompt echoed back / unrecognisable item — nothing to show.
        return []

    return [Notice(subtype=itype, data={})]


def usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    total = getattr(usage, "total", None)
    if total is None:
        return None
    out: dict[str, Any] = {}
    for key, label in (
        ("input_tokens", "input"),
        ("cached_input_tokens", "cached_input"),
        ("output_tokens", "output"),
        ("reasoning_output_tokens", "reasoning_output"),
        ("total_tokens", "total"),
    ):
        value = getattr(total, key, None)
        if value is not None:
            out[label] = value
    return out or None


class CodexBackend(AgentBackend):
    """Runs sessions through the openai-codex SDK (codex app-server)."""

    def __init__(
        self,
        *,
        mcp_servers: dict[str, NeutralServer] | None = None,
        default_cwd: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        effort: str | None = None,
        sandbox: str = "full-access",
        api_key: str | None = None,
    ):
        codex_servers = to_codex_servers(mcp_servers) if mcp_servers else None
        self.thread_config = {"mcp_servers": codex_servers} if codex_servers else None
        self.default_cwd = default_cwd
        self.model = model
        self.max_turns = max_turns
        self.effort = EFFORT_MAP.get(effort) if effort else None
        self.sandbox = SANDBOX_MAP[sandbox]
        self.api_key = api_key
        self.codex: AsyncCodex | None = None

    async def __aenter__(self) -> "CodexBackend":
        self.codex = AsyncCodex()
        await self.codex.__aenter__()
        # The app-server does not read OPENAI_API_KEY from the environment; it
        # authenticates from its own login state (codex home). When a key is
        # provided (e.g. OPENAI_API_KEY in a Cloud Run / container env), log in
        # explicitly. With no key we fall back to whatever login the codex home
        # already holds (e.g. a local ChatGPT login in ~/.codex/auth.json).
        if self.api_key:
            await self.codex.login_api_key(self.api_key)
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        if self.codex is not None:
            await self.codex.__aexit__(*(exc or (None, None, None)))
            self.codex = None
        return False

    async def run_session(
        self,
        prompt: str,
        *,
        system_prompt: str,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if self.codex is None:
            raise RuntimeError("CodexBackend must be entered (async with) before use")

        turn_count = 0
        seen_items: set[str] = set()
        usage = None
        completed_turn = None
        max_turns_hit = False

        try:
            thread = await self.codex.thread_start(
                # deny_all == approval policy "never": nothing pauses for a
                # human, the sandbox preset is the only restriction. This is
                # the Codex equivalent of the Claude path's bypassPermissions.
                approval_mode=ApprovalMode.deny_all,
                developer_instructions=system_prompt,
                cwd=cwd if cwd is not None else self.default_cwd,
                model=self.model,
                sandbox=self.sandbox,
                config=self.thread_config,
            )
            yield SessionStarted(model=self.model or "codex default")

            handle = await thread.turn(prompt, effort=self.effort)

            async for note in handle.stream():
                payload = note.payload

                if isinstance(
                    payload,
                    (
                        models.ItemStartedNotification,
                        models.ItemCompletedNotification,
                    ),
                ):
                    item = root(payload.item)
                    item_id = getattr(item, "id", None)
                    if item_id not in seen_items:
                        seen_items.add(item_id)
                        turn_count += 1
                        yield TurnStart(turn=turn_count)
                        # Approximate max_turns: every item the agent produces
                        # (message, command, tool call) counts as one turn.
                        if (
                            self.max_turns
                            and turn_count > self.max_turns
                            and not max_turns_hit
                        ):
                            max_turns_hit = True
                            try:
                                await handle.interrupt()
                            except CodexError:
                                pass
                    if isinstance(payload, models.ItemStartedNotification):
                        for event in item_started_events(item):
                            yield event
                    else:
                        for event in item_completed_events(item):
                            yield event

                elif isinstance(payload, models.ThreadTokenUsageUpdatedNotification):
                    usage = payload.token_usage

                elif isinstance(payload, models.TurnCompletedNotification):
                    completed_turn = payload.turn

                elif isinstance(payload, models.ErrorNotification):
                    message = getattr(payload.error, "message", str(payload.error))
                    subtype = "error (will retry)" if payload.will_retry else "error"
                    yield Notice(subtype=subtype, data={"message": message})

                # Delta notifications and the rest are intentionally ignored —
                # items are reported whole, like the Claude path reports
                # per-block.

        except (CodexError, RuntimeError, OSError) as exc:
            # A beta SDK / transport failure shouldn't kill the whole run:
            # surface it as an error result and let the caller move on.
            yield Result(
                num_turns=turn_count,
                usage=usage_dict(usage),
                is_error=True,
                error=f"codex backend failure: {exc}",
            )
            return

        status = status_value(completed_turn) if completed_turn is not None else None
        error_msg = getattr(getattr(completed_turn, "error", None), "message", None)
        is_error = False
        if max_turns_hit:
            is_error = True
            error_msg = f"max turns exceeded ({self.max_turns}) — turn interrupted"
        elif completed_turn is None or status not in ("completed",):
            is_error = True
            error_msg = error_msg or f"turn ended with status {status!r}"

        yield Result(
            num_turns=turn_count,
            usage=usage_dict(usage),
            is_error=is_error,
            error=error_msg,
        )
