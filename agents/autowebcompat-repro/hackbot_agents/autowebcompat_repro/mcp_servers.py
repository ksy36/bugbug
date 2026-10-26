"""Stdio configs for the DevTools MCP servers the agent drives.

The MCP servers are npm packages pinned in ``package.json`` and installed into
the image with ``npm ci`` (see the Dockerfile).
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpServerConfig, McpStdioServerConfig

from . import android
from .android import CHROME_DEVTOOLS_PORT, FIREFOX_MARIONETTE_PORT


def resolve_bin(bin_name: str) -> str:
    """Resolve an installed MCP server binary to an absolute path."""
    binary = Path("/app/repro") / "node_modules" / ".bin" / bin_name
    if not binary.exists():
        raise RuntimeError(
            f"MCP server binary not found at {binary}; the image should install "
            f"it with `npm ci` (see the Dockerfile)."
        )
    return str(binary)


def build_firefox_devtools_server(
    firefox_path: Path | None = None,
    *,
    headless: bool = True,
    enable_script: bool = True,
    enable_privileged_context: bool = False,
    profile_path: Path | None = None,
) -> McpStdioServerConfig:
    """Build the stdio config for the Firefox DevTools MCP server.

    Args:
        firefox_path: Firefox binary to drive. When ``None`` the server
            auto-detects an installed Firefox.
        headless: Run Firefox without a visible window (required in
            container/CI environments).
        enable_script: Expose the ``evaluate_script`` tool, which runs
            arbitrary JS in the page context.
        enable_privileged_context: Expose the privileged-context tools
            (``list_extensions``, ``evaluate_privileged_script``, prefs, etc.)
            and set ``MOZ_REMOTE_ALLOW_SYSTEM_ACCESS=1`` on the Firefox process.
            Required for the Chrome Mask flow: the agent needs ``list_extensions``
            to resolve the extension's ``moz-extension://<uuid>/`` base URL, and
            navigating to that privileged origin is itself blocked without this.
        profile_path: A pre-built Firefox profile to use as a template (e.g.
            one with the Chrome Mask extension installed). geckodriver copies
            it into a fresh per-session profile, so the template is not
            mutated. When ``None`` the server uses a clean throwaway profile.
    """
    args = []
    if headless:
        args.append("--headless")
    if enable_script:
        args.append("--enable-script")
    if enable_privileged_context:
        args.append("--enable-privileged-context")
    if firefox_path is not None:
        args += ["--firefox-path", str(firefox_path)]
    if profile_path is not None:
        args += ["--profile-path", str(profile_path)]

    command = resolve_bin("firefox-devtools-mcp-moz")
    if enable_privileged_context:
        return McpStdioServerConfig(
            command=command, args=args, env={"MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"}
        )
    return McpStdioServerConfig(command=command, args=args)


def build_android_firefox_devtools_server(
    *,
    enable_script: bool = True,
) -> McpStdioServerConfig:
    """Attach to Android Firefox launched by ``android.prepare_firefox``.

    Forwarded Marionette and BiDi ports make the running Android browser
    reachable through MCP's existing-instance mode. Omit Android launch
    capabilities so geckodriver does not restart the app or manage its profile.
    """
    args = ["--connect-existing", "--marionette-port", str(FIREFOX_MARIONETTE_PORT)]
    if enable_script:
        args.append("--enable-script")
    return McpStdioServerConfig(
        command=resolve_bin("firefox-devtools-mcp-moz"),
        args=args,
        env={"HOME": str(Path.home()), "TMPDIR": tempfile.gettempdir()},
    )


def build_android_chrome_devtools_server(
    ws_endpoint: str | None = None, *, log_path: Path | None = None
) -> McpStdioServerConfig:
    """Build the stdio config for the Chrome DevTools MCP in Android mode.

    Unlike desktop, the MCP server cannot launch Chrome for Android: the
    browser must already be running with its DevTools socket forwarded to a
    host port (see ``android.forward_chrome_devtools``), and the server
    attaches to it over that websocket.

    Args:
        ws_endpoint: An already discovered DevTools websocket URL. When omitted,
            discover it over HTTP on connection, after ``start_chrome`` has run.
        log_path: Save connection and CDP diagnostics for a reproduction run.
    """
    args = [
        # Opt out of the MCP server's own data collection, as on desktop.
        "--usageStatistics=false",
        "--performanceCrux=false",
    ]
    if ws_endpoint is None:
        args += ["--browserUrl", f"http://127.0.0.1:{CHROME_DEVTOOLS_PORT}"]
    else:
        args += ["--wsEndpoint", ws_endpoint]
    env = {}
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        args += ["--logFile", str(log_path)]
        env["DEBUG"] = "puppeteer:*"
    return McpStdioServerConfig(
        command=resolve_bin("chrome-devtools-mcp"), args=args, env=env
    )


def build_android_browser_server() -> McpServerConfig:
    """Expose native Chrome startup without starting it during Firefox setup."""

    @tool(
        "start_chrome",
        "Start or restart Chrome for Android and wait for its DevTools connection. "
        "Call immediately before the Chrome cross-check, after investigating Firefox. "
        "A restart invalidates existing Chrome page IDs and snapshots; use new_page afterward.",
        {},
    )
    async def start_chrome(args: dict) -> dict:
        try:
            endpoint = await asyncio.to_thread(android.start_chrome)
        except (android.AndroidError, subprocess.TimeoutExpired) as exc:
            return {
                "content": [{"type": "text", "text": f"Chrome startup failed: {exc}"}],
                "is_error": True,
            }
        return {
            "content": [{
                "type": "text",
                "text": f"Chrome DevTools ready at {endpoint}. Use new_page to begin the cross-check.",
            }],
        }

    return create_sdk_mcp_server(name="android-browser", tools=[start_chrome])


def build_chrome_devtools_server(
    chrome_path: Path | None = None,
    *,
    headless: bool = True,
    no_sandbox: bool = True,
) -> McpStdioServerConfig:
    """Build the stdio config for the Chrome DevTools MCP server.

    Args:
        chrome_path: Chrome binary to drive (the Chrome for Testing build from
            ``browser.install_chrome``). When ``None`` the server lets its
            bundled Puppeteer discover a Chrome installation itself.
        headless: Run Chrome without a visible window (required in
            container/CI environments).
        no_sandbox: Pass ``--no-sandbox`` to Chrome. Required when running as an
            unprivileged user inside a container, where Chrome's setuid sandbox
            cannot initialize and the browser otherwise fails to launch.
    """
    args = []
    if headless:
        args.append("--headless")
    if chrome_path is not None:
        args += ["--executablePath", str(chrome_path)]

    # Opt out of the MCP server's own data collection: its usage statistics and
    # the CrUX API calls that send performance-trace URLs to Google. This does
    # not touch Chrome's own behavior, only what the MCP server itself reports.
    args += ["--usageStatistics=false", "--performanceCrux=false"]

    if no_sandbox:
        args += ["--chromeArg=--no-sandbox", "--chromeArg=--disable-setuid-sandbox"]

    return McpStdioServerConfig(command=resolve_bin("chrome-devtools-mcp"), args=args)
