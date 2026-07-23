"""Credentials the runtime provides to agents.

The runtime owns where credentials come from so agents don't reach into the
environment themselves. Anthropic and OpenAI are wired; the :class:`Provider`
protocol leaves room to add others (Vertex, ...) without changing the
agent-facing surface.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """A required credential for a provider is missing or invalid."""


@runtime_checkable
class Provider(Protocol):
    """A credentialed model/service provider the runtime can hand to an agent."""

    name: str

    @property
    def api_key(self) -> str: ...


class AnthropicAuth:
    """Anthropic credentials, read from the environment and validated on access.

    Exposes the API key explicitly (rather than relying on the SDK implicitly
    reading the env) so a missing key fails fast with a clear message instead of
    surfacing as an opaque error deep inside a request.
    """

    name = "anthropic"
    env_var = "ANTHROPIC_API_KEY"

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.env_var)
        if not key:
            raise ProviderError(
                f"{self.env_var} is not set; the runtime cannot provide "
                "Anthropic credentials to this agent."
            )
        return key


class OpenAIAuth:
    """OpenAI credentials for the Codex backend, read from the environment.

    Mirrors :class:`AnthropicAuth`: exposes the key explicitly so a missing
    credential fails fast with a clear message rather than surfacing as an
    opaque error deep inside a request.
    """

    name = "openai"
    env_var = "OPENAI_API_KEY"

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.env_var)
        if not key:
            raise ProviderError(
                f"{self.env_var} is not set; the runtime cannot provide "
                "OpenAI credentials to this agent."
            )
        return key
