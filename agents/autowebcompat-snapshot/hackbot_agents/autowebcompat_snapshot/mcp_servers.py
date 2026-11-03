"""Stdio configs for the DevTools MCP servers the agent drives.

The MCP servers are npm packages pinned in ``package.json`` and installed into
the image with ``npm ci`` (see the Dockerfile).
"""

from pathlib import Path

from claude_agent_sdk.types import McpStdioServerConfig

WORK_DIR = Path("/app/snapshot")


def resolve_bin(bin_name: str) -> str:
    """Resolve an installed MCP server binary to an absolute path."""
    binary = WORK_DIR / "node_modules" / ".bin" / bin_name
    if not binary.exists():
        raise RuntimeError(
            f"MCP server binary not found at {binary}; the image should install "
            f"it with `npm ci` (see the Dockerfile)."
        )
    return str(binary)


def build_firefox_devtools_server(
    firefox_path: Path, *, headless: bool
) -> McpStdioServerConfig:
    args = ["--enable-script", "--firefox-path", str(firefox_path)]
    if headless:
        args.append("--headless")
    return McpStdioServerConfig(
        command=resolve_bin("firefox-devtools-mcp-moz"), args=args
    )
