"""Claude Agent SDK backend — the default agent engine.

Wraps ``ClaudeAgentOptions`` + ``ClaudeSDKClient`` behind the
:class:`AgentBackend` interface and translates the SDK's message stream into
the neutral events in :mod:`hackbot_runtime.backends.base`. The option values
passed here are exactly what agents used to build directly, so the Claude path
behaves as it always has.

Requires the ``claude-sdk`` optional extra of hackbot-runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    McpServerConfig,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import McpStdioServerConfig

from .base import (
    AgentBackend,
    AgentEvent,
    AssistantText,
    HttpServer,
    NeutralServer,
    Notice,
    Result,
    SessionStarted,
    StdioServer,
    Thinking,
    ToolCall,
    ToolResult,
    TurnStart,
)


def tool_result_text(content: Any) -> str:
    """Normalise a ToolResultBlock's content (str or content list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def to_claude_server(server: NeutralServer) -> McpServerConfig:
    """Convert a neutral server descriptor into the Claude SDK's config."""
    if isinstance(server, HttpServer):
        config: dict[str, Any] = {"type": "http", "url": server.url}
        if server.headers:
            config["headers"] = server.headers
        return config  # type: ignore[return-value]
    if server.env:
        return McpStdioServerConfig(
            type="stdio", command=server.command, args=server.args, env=server.env
        )
    return McpStdioServerConfig(type="stdio", command=server.command, args=server.args)


def resolve_servers(
    servers: dict[str, Any] | None,
) -> dict[str, McpServerConfig]:
    """Convert any neutral descriptors in ``servers`` to Claude config.

    Native Claude ``McpServerConfig`` values (e.g. the in-process result
    server built with ``create_sdk_mcp_server``) are passed through unchanged.
    """
    resolved: dict[str, McpServerConfig] = {}
    for name, server in (servers or {}).items():
        if isinstance(server, (StdioServer, HttpServer)):
            resolved[name] = to_claude_server(server)
        else:
            resolved[name] = server
    return resolved


class ClaudeBackend(AgentBackend):
    """Runs sessions through ``ClaudeSDKClient``.

    One fresh client (and therefore one fresh agent context) per
    ``run_session`` call. In-process MCP servers and option values are shared
    across sessions.
    """

    def __init__(
        self,
        *,
        mcp_servers: dict[str, Any] | None = None,
        allowed_tools: list[str],
        disallowed_tools: list[str] | None = None,
        agents: dict[str, Any] | None = None,
        default_cwd: str | None = None,
        add_dirs: list[str] | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        effort: str | None = None,
        max_buffer_size: int | None = None,
    ):
        self.mcp_servers = resolve_servers(mcp_servers)
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools or []
        self.agents = agents
        self.default_cwd = default_cwd
        self.add_dirs = add_dirs or []
        self.model = model
        self.max_turns = max_turns
        self.effort = effort
        self.max_buffer_size = max_buffer_size

    def build_options(self, system_prompt: str, cwd: str | None) -> ClaudeAgentOptions:
        kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            mcp_servers=self.mcp_servers,
            permission_mode="bypassPermissions",
            allowed_tools=self.allowed_tools,
            disallowed_tools=self.disallowed_tools,
            model=self.model,
            max_turns=self.max_turns,
            # Don't inherit user/project settings — stay self-contained.
            setting_sources=[],
        )
        if self.agents is not None:
            kwargs["agents"] = self.agents
        if cwd is not None:
            kwargs["cwd"] = cwd
        if self.add_dirs:
            kwargs["add_dirs"] = self.add_dirs
        # Newer models require `effort` (adaptive thinking) rather than the
        # legacy thinking.type=enabled config. Only pass it when explicitly
        # configured so older models keep working with the SDK default.
        if self.effort:
            kwargs["effort"] = self.effort
        # DevTools snapshots of complex pages can exceed the SDK's default
        # 1 MiB message buffer (the reader dies fatally if it does).
        if self.max_buffer_size:
            kwargs["max_buffer_size"] = self.max_buffer_size
        return ClaudeAgentOptions(**kwargs)

    async def run_session(
        self,
        prompt: str,
        *,
        system_prompt: str,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        options = self.build_options(
            system_prompt, cwd if cwd is not None else self.default_cwd
        )
        turn = 0

        # ClaudeSDKClient rather than the top-level query() helper: with a
        # string prompt + SDK MCP servers, query() awaits the final result
        # *before* it starts draining the message buffer, so a run producing
        # many messages deadlocks. The client keeps stdin open and drains as
        # it goes.
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    subagent = msg.parent_tool_use_id is not None
                    if not subagent:
                        # Each top-level AssistantMessage is one agent turn.
                        # Subagent turns don't advance the counter — they nest
                        # inside a Task call that belongs to one main turn.
                        turn += 1
                        yield TurnStart(turn=turn)
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield AssistantText(text=block.text, subagent=subagent)
                        elif isinstance(block, ThinkingBlock):
                            yield Thinking(text=block.thinking, subagent=subagent)
                        elif isinstance(block, ToolUseBlock):
                            yield ToolCall(
                                name=block.name,
                                input=block.input,
                                subagent=subagent,
                            )

                elif isinstance(msg, UserMessage):
                    # Tool results arrive wrapped in UserMessage.
                    if isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                yield ToolResult(
                                    text=tool_result_text(block.content),
                                    is_error=bool(block.is_error),
                                )

                elif isinstance(msg, SystemMessage):
                    if msg.subtype == "init":
                        yield SessionStarted(model=msg.data.get("model", "?"))
                    else:
                        yield Notice(subtype=msg.subtype, data=msg.data)

                elif isinstance(msg, ResultMessage):
                    yield Result(
                        num_turns=msg.num_turns,
                        total_cost_usd=msg.total_cost_usd,
                        is_error=msg.is_error,
                        error=msg.result if msg.is_error else None,
                    )
