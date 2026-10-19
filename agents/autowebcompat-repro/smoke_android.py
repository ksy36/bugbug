"""Smoke test: can both DevTools MCPs actually drive their browser on the emulator?

Boots the emulator, installs Fenix, then speaks MCP stdio to each server and
checks navigate + snapshot + userAgent round-trip. Exits non-zero on failure.

Run inside the agent image; see .github/workflows/android-emulator-smoke.yml.
"""

import asyncio
import json
import logging
import sys

from hackbot_agents.autowebcompat_repro import android
from hackbot_agents.autowebcompat_repro.android_browser import install_fenix
from hackbot_agents.autowebcompat_repro.mcp_servers import (
    build_android_chrome_devtools_server,
    build_android_firefox_devtools_server,
)

URL = "https://example.com/"


async def mcp_call(proc, method, params, req_id, timeout=180):
    proc.stdin.write(
        (
            json.dumps(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            )
            + "\n"
        ).encode()
    )
    await proc.stdin.drain()
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout)
        if not line:
            raise RuntimeError(f"{method}: server closed stdout")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == req_id:
            return msg


def tool_text(resp):
    if "error" in resp:
        raise RuntimeError(f"MCP error: {json.dumps(resp['error'])[:300]}")
    result = resp.get("result", {})
    text = "\n".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        raise RuntimeError(f"tool reported failure: {text[:300]}")
    return text


async def drive(label, config, expect_ua):
    """Run navigate/snapshot/userAgent against one MCP server."""
    proc = await asyncio.create_subprocess_exec(
        config["command"],
        *config["args"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**__import__("os").environ, **config.get("env", {})},
    )
    try:
        await mcp_call(
            proc,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"},
            },
            1,
        )
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()

        nav = tool_text(
            await mcp_call(
                proc,
                "tools/call",
                {"name": "navigate_page", "arguments": {"url": URL}},
                2,
            )
        )
        print(f"[{label}] navigate: {nav.strip()[:120]}")

        snap = tool_text(
            await mcp_call(
                proc, "tools/call", {"name": "take_snapshot", "arguments": {}}, 3
            )
        )
        # Assert on structure, not page copy: example.com's text changes, and
        # the two MCPs format snapshots differently. A uid means the
        # accessibility tree came back and the agent has something to click.
        if "uid=" not in snap:
            raise RuntimeError(f"snapshot has no uids: {snap[:300]}")
        print(f"[{label}] snapshot: {snap.count('uid=')} uids")

        ua = tool_text(
            await mcp_call(
                proc,
                "tools/call",
                {
                    "name": "evaluate_script",
                    "arguments": {"function": "() => navigator.userAgent"},
                },
                4,
            )
        )
        if expect_ua not in ua:
            raise RuntimeError(f"UA missing {expect_ua!r}: {ua[:300]}")
        print(f"[{label}] userAgent contains {expect_ua!r}")
    finally:
        proc.terminate()
        await proc.wait()


async def main():
    with android.Emulator():
        package = install_fenix("nightly")

        # Diagnostics: the fchown failure geckodriver reports when pushing a
        # Fenix profile depends on who adbd thinks we are and where we stage
        # files, so record both before driving anything.
        import os

        print(
            f"[env] uid={os.getuid()} home={os.path.expanduser('~')} tmp={os.environ.get('TMPDIR', '/tmp')}"
        )
        print(f"[env] adb root: {android.adb('root', check=False).strip()!r}")
        print(
            f"[env] debuggable: {android.getprop('ro.debuggable')!r} "
            f"build type: {android.getprop('ro.build.type')!r}"
        )

        android.start_app(package)
        android.start_app(android.CHROME_PACKAGE)
        endpoint = android.forward_chrome_devtools()

        ff = build_android_firefox_devtools_server(package, android.DEVICE_SERIAL)
        await drive("fenix", ff, "Firefox")

        chrome = build_android_chrome_devtools_server(endpoint)
        await drive("chrome", chrome, "Chrome")

    print("SMOKE OK: both MCPs drove their browser on the emulator")


if __name__ == "__main__":
    # Without this the module's logger.info calls (emulator boot progress,
    # installs) are swallowed, which makes a CI timeout undiagnosable.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout
    )
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
