"""Exercise real Android browsers through MCP without an AI API key.

Run inside the android-agent Docker target with /dev/kvm passed through.
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from hackbot_agents.autowebcompat_repro import android
from hackbot_agents.autowebcompat_repro.android_browser import (
    AndroidBrowser,
    AndroidEnvironment,
    AndroidProfile,
)
from hackbot_agents.autowebcompat_repro.mcp_servers import (
    build_chrome_devtools_server,
    build_firefox_devtools_server,
)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ARTIFACTS = Path(os.environ.get("SMOKE_ARTIFACTS_DIR", "/tmp/android-smoke"))


@asynccontextmanager
async def connect(config):
    parameters = StdioServerParameters(
        command=config["command"], args=config["args"], env=config.get("env")
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(
            reader, writer, read_timeout_seconds=timedelta(seconds=300)
        ) as session,
    ):
        await session.initialize()
        yield session


async def call(session, tool_name, **arguments):
    result = await session.call_tool(
        tool_name, arguments, read_timeout_seconds=timedelta(seconds=300)
    )
    text = "\n".join(block.text for block in result.content if block.type == "text")
    if result.isError:
        raise RuntimeError(f"{tool_name} failed: {text}")
    return text


async def firefox(channel, mask=False):
    label = f"firefox-{channel}" + ("-chrome-mask" if mask else "")
    print(f"[{label}] starting", flush=True)
    config = build_firefox_devtools_server(
        AndroidBrowser("firefox", channel),
        enable_privileged_context=mask,
        profile_path=AndroidProfile(("chrome-mask",) if mask else ()),
    )
    async with connect(config) as session:
        if mask:
            extensions = await call(session, "list_extensions", name="Chrome Mask")
            match = re.search(r"UUID:\s*([\w-]+)", extensions)
            if not match or "Active: yes" not in extensions:
                raise RuntimeError(f"Chrome Mask not active: {extensions}")
            await call(
                session, "navigate_page", url=f"moz-extension://{match[1]}/options.html"
            )
            await call(
                session,
                "evaluate_script",
                function="""() => {
                const input = document.querySelector('#add-site-input');
                if (!input) throw new Error('Chrome Mask Add Site form missing');
                input.value = 'example.com';
                document.querySelector('#add-site-form').requestSubmit();
                return 'submitted';
            }""",
            )
            await asyncio.sleep(2)
        await call(session, "navigate_page", url="https://example.com/")
        ua = await call(
            session, "evaluate_script", function="() => navigator.userAgent"
        )
        expected = "Chrome" if mask else "Firefox"
        if expected not in ua or "Android" not in ua:
            raise RuntimeError(f"Expected Android {expected} user agent: {ua}")
        await call(session, "take_snapshot")
        await call(session, "screenshot_page", saveTo=str(ARTIFACTS / f"{label}.png"))
    print(f"[{label}] PASS", flush=True)


async def chrome():
    print("[chrome] starting", flush=True)
    async with connect(
        build_chrome_devtools_server(AndroidBrowser("chrome"))
    ) as session:
        await call(session, "new_page", url="https://example.com/")
        ua = await call(
            session, "evaluate_script", function="() => navigator.userAgent"
        )
        if "Chrome" not in ua or "Android" not in ua:
            raise RuntimeError(f"Expected Android Chrome user agent: {ua}")
        await call(session, "take_snapshot")
        await call(session, "take_screenshot", filePath=str(ARTIFACTS / "chrome.png"))
    print("[chrome] PASS", flush=True)


async def main(channels):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    failed = []
    try:
        with AndroidEnvironment():
            for channel in channels:
                # Bind channel per iteration: a bare closure would capture the
                # loop variable and run every check against the last channel.
                for label, check in (
                    (f"firefox-{channel}", lambda c=channel: firefox(c)),
                    ("chrome", chrome),
                    (f"chrome-mask-{channel}", lambda c=channel: firefox(c, mask=True)),
                ):
                    try:
                        await check()
                    except Exception as exc:
                        failed.append(label)
                        print(f"[{label}] FAIL: {exc}", flush=True)
            (ARTIFACTS / "logcat.txt").write_text(
                android.adb("logcat", "-d", check=False)
            )
    finally:
        log = Path("/tmp/android-emulator.log")
        if log.exists():
            shutil.copyfile(log, ARTIFACTS / log.name)
    if failed:
        raise RuntimeError(f"Android smoke tests failed: {', '.join(failed)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", action="append", choices=("nightly", "stable"))
    options = parser.parse_args()
    asyncio.run(main(options.channel or ["nightly", "stable"]))
