"""Backend-neutral agent interface.

Agents drive an LLM through one of several SDK backends (the Claude Agent SDK
by default, OpenAI Codex with the ``codex`` backend). A backend turns one user
prompt into a streamed sequence of the neutral events defined here; the shared
``Reporter`` renders those events, so on-screen / log output is identical
regardless of backend.

This mirrors the backend split proven in the ``larrey`` project: define the
tool surface and orchestration once, and let each backend translate its SDK's
native message stream into these events.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass
class SessionStarted:
    """The agent session is up; ``model`` is whatever the backend reports."""

    model: str


@dataclass
class TurnStart:
    """A new agent turn began. ``turn`` counts from 1 within one session.

    Claude: one turn per main-agent assistant message. Codex has no exact
    equivalent; its backend counts one turn per item the agent produces
    (message, command, tool call), which is comparable granularity.
    """

    turn: int


@dataclass
class AssistantText:
    """Text the agent addressed to the operator."""

    text: str
    subagent: bool = False


@dataclass
class Thinking:
    """Internal reasoning emitted by the model (verbose / log only)."""

    text: str
    subagent: bool = False


@dataclass
class ToolCall:
    """The agent invoked a tool."""

    name: str
    input: dict[str, Any] = field(default_factory=dict)
    subagent: bool = False


@dataclass
class ToolResult:
    """A tool finished; ``text`` is its (textual) output."""

    text: str
    is_error: bool = False


@dataclass
class Notice:
    """Out-of-band backend information (init details, warnings, ...)."""

    subtype: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    """End of one session.

    ``total_cost_usd`` is only available from the Claude backend; ``usage``
    (token counts) only from the Codex backend. Consumers that care about spend
    should treat both as optional and fall back to whichever the active backend
    supplies.
    """

    num_turns: int
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    is_error: bool = False
    error: str | None = None


AgentEvent = (
    SessionStarted
    | TurnStart
    | AssistantText
    | Thinking
    | ToolCall
    | ToolResult
    | Notice
    | Result
)


# --------------------------------------------------------------------------- #
# Tool + server descriptors (backend-neutral)
# --------------------------------------------------------------------------- #


@dataclass
class StdioServer:
    """A backend-neutral description of an external stdio MCP server.

    Each backend converts this into its own config shape: the Claude backend
    into ``McpStdioServerConfig``, the Codex backend into an entry of the
    ``mcp_servers`` config dict its app-server consumes. The command/args/env
    themselves never change between backends.
    """

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class HttpServer:
    """A backend-neutral description of a remote (HTTP) MCP server.

    Used for servers reached over the network rather than spawned as a child
    process — e.g. the Bugzilla broker sidecar. Each backend converts this into
    its own remote-server config shape.
    """

    url: str
    headers: dict[str, str] = field(default_factory=dict)


NeutralServer = StdioServer | HttpServer


# --------------------------------------------------------------------------- #
# Backend interface
# --------------------------------------------------------------------------- #


class AgentBackend(ABC):
    """One agent engine (Claude, Codex, ...).

    Backends are async context managers so they can hold per-run state (e.g.
    the Codex app-server process) across multiple sessions. One
    ``run_session`` call is one isolated agent context.
    """

    async def __aenter__(self) -> "AgentBackend":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    @abstractmethod
    def run_session(
        self,
        prompt: str,
        *,
        system_prompt: str,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one agent session and yield events until it completes.

        ``cwd`` overrides the backend's default working directory for this
        session only.
        """
