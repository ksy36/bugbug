"""Shared claude-agent-sdk helpers for hackbot agents.

Generic, agent-neutral building blocks that every claude-agent-sdk agent would
otherwise copy verbatim. Agents still assemble their own ``ClaudeAgentOptions``
and drive the ``ClaudeSDKClient`` loop — these just remove the boilerplate of
rendering the streamed messages.

Requires the ``claude-sdk`` optional extra of hackbot-runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from hackbot_runtime.backends import base as events


def _truncate(s: str, n: int = 500) -> str:
    return s if len(s) <= n else s[:n] + f"... [{len(s) - n} more chars]"


class Reporter:
    """Routes streamed claude-agent-sdk messages to stdout and/or a log file."""

    def __init__(self, verbose: bool, log_path: Path | None):
        self.verbose = verbose
        self._log = log_path.open("w", encoding="utf-8") if log_path else None
        self._turn = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._log:
            self._log.close()

    def header(self, title: str) -> None:
        """Emit a section header (e.g. ``"bug 12345"``) and reset the turn count."""
        self._turn = 0
        banner = f"\n{'#' * 60}\n# {title}\n{'#' * 60}"
        self._emit(banner, always=True)

    def _emit(self, line: str, *, always: bool = False, full: str | None = None):
        if self._log:
            self._log.write((full if full is not None else line) + "\n")
            self._log.flush()
        if always or self.verbose:
            print(line)

    def message(self, msg) -> None:
        if isinstance(msg, AssistantMessage):
            is_main = msg.parent_tool_use_id is None
            label = "agent" if is_main else "subagent"
            if is_main:
                self._turn += 1
                self._emit(f"\n--- turn {self._turn} ---")
            for block in msg.content:
                if isinstance(block, TextBlock):
                    self._emit(f"\n[{label}] {block.text}", always=is_main)
                elif isinstance(block, ThinkingBlock):
                    thinking = block.thinking.strip()
                    snippet = thinking.split("\n", 1)[0]
                    self._emit(
                        f"[{label}:thinking] {_truncate(snippet, 120)}",
                        full=f"[{label}:thinking]\n{thinking}",
                    )
                elif isinstance(block, ToolUseBlock):
                    inp = json.dumps(block.input, default=str)
                    inp_full = json.dumps(block.input, indent=2, default=str)
                    self._emit(
                        f"[{label}→tool] {block.name}({_truncate(inp, 300)})",
                        full=f"[{label}→tool] {block.name}\n{inp_full}",
                    )

        elif isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        marker = "ERROR" if block.is_error else "ok"
                        if isinstance(block.content, str):
                            text = block.content
                        elif isinstance(block.content, list):
                            parts = [
                                c.get("text", "")
                                for c in block.content
                                if isinstance(c, dict) and c.get("type") == "text"
                            ]
                            text = "\n".join(parts)
                        else:
                            text = str(block.content)
                        self._emit(
                            f"  [tool←{marker}] {_truncate(text, 400)}",
                            full=f"  [tool←{marker}]\n{text}",
                        )

        elif isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                model = msg.data.get("model", "?")
                self._emit(f"[system] session started (model={model})")
            else:
                data = json.dumps(msg.data, default=str)
                self._emit(
                    f"[system:{msg.subtype}] {_truncate(data, 200)}",
                    full=f"[system:{msg.subtype}] {data}",
                )

        elif isinstance(msg, ResultMessage):
            self._emit(f"\n{'=' * 60}", always=True)
            if msg.total_cost_usd:
                line = f"[done] turns={msg.num_turns} cost=${msg.total_cost_usd:.4f}"
            else:
                line = f"[done] turns={msg.num_turns}"
            self._emit(line, always=True)
            if msg.is_error:
                self._emit(f"[done] ERROR: {msg.result}", always=True)

    def event(self, ev: events.AgentEvent) -> None:
        """Render a backend-neutral :class:`AgentEvent`.

        Mirrors :meth:`message` but consumes the events emitted by any
        backend (Claude or Codex), so on-screen / log output is identical
        regardless of engine.
        """
        if isinstance(ev, events.SessionStarted):
            self._emit(f"[system] session started (model={ev.model})")

        elif isinstance(ev, events.TurnStart):
            self._turn = ev.turn
            self._emit(f"\n--- turn {self._turn} ---")

        elif isinstance(ev, events.AssistantText):
            label = "subagent" if ev.subagent else "agent"
            self._emit(f"\n[{label}] {ev.text}", always=not ev.subagent)

        elif isinstance(ev, events.Thinking):
            label = "subagent" if ev.subagent else "agent"
            thinking = ev.text.strip()
            snippet = thinking.split("\n", 1)[0]
            self._emit(
                f"[{label}:thinking] {_truncate(snippet, 120)}",
                full=f"[{label}:thinking]\n{thinking}",
            )

        elif isinstance(ev, events.ToolCall):
            label = "subagent" if ev.subagent else "agent"
            inp = json.dumps(ev.input, default=str)
            inp_full = json.dumps(ev.input, indent=2, default=str)
            self._emit(
                f"[{label}→tool] {ev.name}({_truncate(inp, 300)})",
                full=f"[{label}→tool] {ev.name}\n{inp_full}",
            )

        elif isinstance(ev, events.ToolResult):
            marker = "ERROR" if ev.is_error else "ok"
            self._emit(
                f"  [tool←{marker}] {_truncate(ev.text, 400)}",
                full=f"  [tool←{marker}]\n{ev.text}",
            )

        elif isinstance(ev, events.Notice):
            data = json.dumps(ev.data, default=str)
            self._emit(
                f"[system:{ev.subtype}] {_truncate(data, 200)}",
                full=f"[system:{ev.subtype}] {data}",
            )

        elif isinstance(ev, events.Result):
            self._emit(f"\n{'=' * 60}", always=True)
            if ev.total_cost_usd:
                line = f"[done] turns={ev.num_turns} cost=${ev.total_cost_usd:.4f}"
            elif ev.usage:
                line = f"[done] turns={ev.num_turns} usage={ev.usage}"
            else:
                line = f"[done] turns={ev.num_turns}"
            self._emit(line, always=True)
            if ev.is_error:
                self._emit(f"[done] ERROR: {ev.error}", always=True)
