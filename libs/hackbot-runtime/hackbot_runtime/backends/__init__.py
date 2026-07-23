"""Agent SDK backends (see :mod:`hackbot_runtime.backends.base`).

The concrete backends are imported lazily so that selecting one SDK does not
require the other to be importable: ``ClaudeBackend`` needs the ``claude-sdk``
extra, ``CodexBackend`` needs the ``codex-sdk`` extra.
"""

from __future__ import annotations

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

__all__ = [
    "AgentBackend",
    "AgentEvent",
    "AssistantText",
    "HttpServer",
    "NeutralServer",
    "Notice",
    "Result",
    "SessionStarted",
    "StdioServer",
    "Thinking",
    "ToolCall",
    "ToolResult",
    "TurnStart",
    "load_backend",
]


def load_backend(name: str) -> type[AgentBackend]:
    """Import and return a backend class by name (``"claude"`` / ``"codex"``).

    Imported lazily so an agent that only installs one SDK extra never tries
    to import the other.
    """
    if name == "claude":
        from .claude_backend import ClaudeBackend

        return ClaudeBackend
    if name == "codex":
        from .codex_backend import CodexBackend

        return CodexBackend
    raise ValueError(f"unknown backend {name!r} (expected 'claude' or 'codex')")
