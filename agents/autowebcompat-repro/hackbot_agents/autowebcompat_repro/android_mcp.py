"""Activate Android browsers and proxy their existing DevTools MCP tools."""

import asyncio
import base64
import contextlib
import json
import logging
import sys
import tempfile
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from claude_agent_sdk.types import McpStdioServerConfig
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .android import CHROME_DEVTOOLS_PORT, FIREFOX_MARIONETTE_PORT
from .android_browser import AndroidBrowser
from .mcp_servers import resolve_bin
from .setup_profile import download, extract_extension_id, resolve_xpi_url

logger = logging.getLogger("autowebcompat-repro")


def build_server(
    browser: AndroidBrowser,
    extensions: Sequence[str],
    bin_name: str,
    args: list[str],
) -> McpStdioServerConfig:
    return McpStdioServerConfig(
        command=sys.executable,
        args=[
            "-m",
            "hackbot_agents.autowebcompat_repro.android_mcp",
            str(browser),
            json.dumps(list(extensions)),
            resolve_bin(bin_name),
            *args,
        ],
    )


def build_android_firefox_devtools_server(
    browser: AndroidBrowser,
    *,
    enable_privileged_context: bool = False,
    extensions: Sequence[str] = (),
) -> McpStdioServerConfig:
    args = [
        "--connect-existing",
        "--marionette-port",
        str(FIREFOX_MARIONETTE_PORT),
        "--enable-script",
    ]
    if enable_privileged_context:
        args.append("--enable-privileged-context")
    return build_server(browser, extensions, "firefox-devtools-mcp-moz", args)


def build_android_chrome_devtools_server(
    browser: AndroidBrowser,
) -> McpStdioServerConfig:
    return build_server(
        browser,
        (),
        "chrome-devtools-mcp",
        [
            "--browserUrl",
            f"http://127.0.0.1:{CHROME_DEVTOOLS_PORT}",
            "--usageStatistics=false",
            "--performanceCrux=false",
        ],
    )


async def install_extension(session, slug):
    """Install a signed AMO addon using the upstream MCP connection."""

    def fetch():
        with tempfile.TemporaryDirectory(prefix="android-addon-") as tmp:
            url, version = resolve_xpi_url(slug)
            path = Path(tmp) / "addon.xpi"
            download(url, path)
            return (
                version,
                extract_extension_id(path),
                base64.b64encode(path.read_bytes()).decode("ascii"),
            )

    version, extension_id, encoded = await asyncio.to_thread(fetch)
    logger.info("Installing Android extension %s %s through Firefox MCP", slug, version)
    result = await session.call_tool(
        "install_extension",
        {
            "type": "base64",
            "value": encoded,
            "permanent": True,
        },
    )
    text = "\n".join(block.text for block in result.content if block.type == "text")
    if result.isError or f"ID: {extension_id}" not in text:
        raise RuntimeError(f"Android extension installation failed: {text}")


class AndroidTools:
    def __init__(self, browser, session, extensions=()):
        self.browser = browser
        self.session = session
        self.extensions = extensions
        self.started = False
        self.installed = False
        self.lock = asyncio.Lock()

    async def call(self, name, arguments):
        async with self.lock:
            # Native helpers log to stderr; stdout belongs to the MCP protocol.
            with contextlib.redirect_stdout(sys.stderr):
                await asyncio.to_thread(self.browser.start, force=not self.started)
            self.started = True
            if not self.installed:
                for slug in self.extensions:
                    await install_extension(self.session, slug)
                self.installed = True
            result = await self.session.call_tool(name, arguments)
            if name == "restart_firefox":
                self.started = False
                with contextlib.redirect_stdout(sys.stderr):
                    await asyncio.to_thread(self.browser.start, force=True)
            return result


async def serve(browser, command, extensions=()):
    parameters = StdioServerParameters(command=command[0], args=command[1:])
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(
            reader, writer, read_timeout_seconds=timedelta(seconds=180)
        ) as session,
    ):
        await session.initialize()
        tools = AndroidTools(browser, session, extensions)
        server = Server("android-devtools")

        @server.list_tools()
        async def list_tools():
            return (await session.list_tools()).tools

        @server.call_tool()
        async def call_tool(name, arguments):
            return await tools.call(name, arguments)

        async with stdio_server() as (incoming, outgoing):
            await server.run(incoming, outgoing, server.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    marker, encoded_extensions, *command = sys.argv[1:]
    asyncio.run(
        serve(AndroidBrowser.parse(marker), command, json.loads(encoded_extensions))
    )
