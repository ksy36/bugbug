"""Smoke test: can both DevTools MCPs actually drive their browser on the emulator?

Boots the emulator, installs Fenix, then speaks MCP stdio to each server and
checks navigate + snapshot + userAgent round-trip. Exits non-zero on failure.

Run inside the agent image; see .github/workflows/android-emulator-smoke.yml.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from urllib.request import urlopen

from hackbot_agents.autowebcompat_repro import android

URL = "https://example.com/"
ARTIFACTS = Path("/tmp/android-smoke-artifacts")


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
    # Notifications must not keep resetting the timeout indefinitely.
    async with asyncio.timeout(timeout):
        while True:
            line = await proc.stdout.readline()
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
        # MCP and geckodriver diagnostics belong in the Actions log.
        stderr=None,
        env={**os.environ, **config.get("env", {})},
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

        if label == "chrome":
            # Connecting to the Android browser endpoint does not guarantee
            # that MCP has a selected page. new_page creates and selects one;
            # navigate_page requires an existing selection.
            page = tool_text(
                await mcp_call(
                    proc,
                    "tools/call",
                    {"name": "new_page", "arguments": {"url": "about:blank"}},
                    5,
                )
            )
            print(f"[{label}] new page: {page.strip()[:120]}")

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
                    "arguments": {
                        "function": "() => ({ userAgent: navigator.userAgent, url: location.href })"
                    },
                },
                4,
            )
        )
        if expect_ua not in ua:
            raise RuntimeError(f"UA missing {expect_ua!r}: {ua[:300]}")
        if "Android" not in ua:
            raise RuntimeError(f"UA is not Android: {ua[:300]}")
        if URL not in ua:
            raise RuntimeError(f"Browser did not reach {URL}: {ua[:300]}")
        if label == "chrome" and "Mobile" not in ua:
            raise RuntimeError(f"Chrome UA is not mobile: {ua[:300]}")
        print(f"[{label}] userAgent contains {expect_ua!r}")
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()


def diagnostics(label):
    """Save device evidence before the emulator is torn down."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if label == "chrome":
        try:
            with urlopen(
                f"http://127.0.0.1:{android.CHROME_DEVTOOLS_PORT}/json/list", timeout=5
            ) as response:
                targets = json.load(response)
            (ARTIFACTS / "chrome-targets.json").write_text(
                json.dumps(targets, indent=2)
            )
        except Exception as exc:
            print(f"[chrome] collecting CDP targets failed: {exc}", file=sys.stderr)
    for name, args in {
        "logcat": ("logcat", "-d"),
        "activities": ("shell", "dumpsys", "activity", "activities"),
        "sockets": ("shell", "cat", "/proc/net/unix"),
        "flags": ("shell", "cat", "/data/local/tmp/chrome-command-line"),
        "chrome-package": ("shell", "dumpsys", "package", android.CHROME_PACKAGE),
    }.items():
        try:
            output = android.adb(*args, check=False, timeout=30)
            (ARTIFACTS / f"{label}-{name}.txt").write_text(output)
        except Exception as exc:
            print(f"[{label}] collecting {name} failed: {exc}", file=sys.stderr)


async def main():
    from hackbot_agents.autowebcompat_repro.android_browser import install_fenix
    from hackbot_agents.autowebcompat_repro.mcp_servers import (
        build_android_chrome_devtools_server,
        build_android_firefox_devtools_server,
    )

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with android.Emulator(log_path=ARTIFACTS / "emulator.log"):
        print(
            f"[env] uid={os.getuid()} home={os.path.expanduser('~')} tmp={os.environ.get('TMPDIR', '/tmp')}"
        )
        print(
            f"[env] debuggable: {android.getprop('ro.debuggable')!r} "
            f"build type: {android.getprop('ro.build.type')!r}"
        )

        failures = []
        # Exercise each browser even if the other fails during setup or MCP
        # control. Otherwise a missing Chrome socket hides all Firefox results.
        for label in ("fenix", "chrome"):
            package = None
            try:
                print(f"[{label}] starting smoke test")
                if label == "fenix":
                    package = install_fenix("nightly")
                    android.prepare_firefox(package)
                    config = build_android_firefox_devtools_server()
                    await drive(label, config, "Firefox")
                else:
                    android.prepare_chrome()
                    print(
                        f"[chrome] controlling Android package {android.CHROME_PACKAGE}"
                    )
                    android.start_app(android.CHROME_PACKAGE, "about:blank")
                    endpoint = android.forward_chrome_devtools()
                    config = build_android_chrome_devtools_server(endpoint)
                    # Keep the CDP exchange: a generic Puppeteer timeout can
                    # otherwise hide a browser/protocol version mismatch.
                    ARTIFACTS.mkdir(parents=True, exist_ok=True)
                    config["args"] += ["--logFile", str(ARTIFACTS / "chrome-mcp.log")]
                    config["env"] = {
                        **config.get("env", {}),
                        "DEBUG": "puppeteer:*",
                    }
                    await drive(label, config, "Chrome")
                print(f"[{label}] PASS")
            except Exception as exc:
                failures.append(label)
                print(f"[{label}] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                diagnostics(label)
                if label == "fenix" and package:
                    # A failed geckodriver launch can leave Firefox starting
                    # asynchronously, stealing focus and memory from Chrome.
                    try:
                        android.adb("shell", "am", "force-stop", package)
                    except android.AndroidError as exc:
                        print(f"[fenix] stopping app failed: {exc}", file=sys.stderr)
        if failures:
            raise RuntimeError(f"Browser smoke tests failed: {', '.join(failures)}")

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
