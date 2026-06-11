"""Firefox DevTools MCP server config for web-compat reproduction.

Wraps @mozilla/firefox-devtools-mcp-moz (Selenium + WebDriver BiDi) as a
stdio MCP server the agent launches as a child process. Only used when
web-compat tools are enabled; the browser holds no secrets, so unlike the
Bugzilla broker there is no out-of-process token isolation here.
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk.types import McpStdioServerConfig

# npm package providing the MCP server binary. Run via npx so we always pick
# up the installed/published version without a separate global install step.
_PACKAGE = "@mozilla/firefox-devtools-mcp-moz"


def build_devtools_server(
    firefox_path: Path | None = None,
    *,
    headless: bool = True,
    enable_script: bool = True,
) -> McpStdioServerConfig:
    """Build the stdio config for the Firefox DevTools MCP server.

    Args:
        firefox_path: Firefox binary to drive. When ``None`` the server
            auto-detects an installed Firefox.
        headless: Run Firefox without a visible window (required in
            container/CI environments).
        enable_script: Expose the ``evaluate_script`` tool, which runs
            arbitrary JS in the page context. Needed to read JS-only state
            such as ``navigator.userAgent`` during web-compat triage. The
            privileged-context tools are intentionally left disabled.
    """
    args = [_PACKAGE]
    if headless:
        args.append("--headless")
    if enable_script:
        args.append("--enable-script")
    if firefox_path is not None:
        args += ["--firefox-path", str(firefox_path)]

    return McpStdioServerConfig(command="npx", args=args)