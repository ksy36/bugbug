"""Smoke test: can both DevTools MCPs actually drive their browser on the emulator?

Boots the emulator, installs Fenix, then speaks MCP stdio to each server and
checks navigation, snapshots, input, keyboard focus, scrolling and screenshots.
Exits non-zero on failure.

Run inside the agent image; see .github/workflows/android-emulator-smoke.yml.
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from hackbot_agents.autowebcompat_repro import android

URL = "https://example.com/"
ARTIFACTS = Path("/tmp/android-smoke-artifacts")

# Inspired by https://bugzilla.mozilla.org/show_bug.cgi?id=2015504.
# Keep this offline so site changes and consent dialogs cannot mask MCP failures.
INTERACTION_PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-visual">
<title>Android MCP interactions</title>
<style>
  body { margin: 0; font: 18px sans-serif; }
  main { min-height: 400vh; padding: 20px; background: linear-gradient(#def, #fed); }
  footer { position: fixed; bottom: 0; left: 0; right: 0; padding: 12px; background: white; }
  input, button { font: inherit; padding: 8px; max-width: 65%; }
</style>
<main><h1>Android MCP interactions</h1><p>Scroll down and back with the keyboard open.</p></main>
<footer>
  <input id="prompt" aria-label="Smoke prompt" placeholder="Type here">
  <button type="button" onclick="document.body.dataset.submitted = document.querySelector('#prompt').value">Check input</button>
</footer></html>"""


def snapshot_uid(snapshot, name):
    """Find a named control in either MCP's accessibility snapshot."""
    for line in snapshot.splitlines():
        if f'"{name}"' in line:
            match = re.search(r'\buid=["\']?([\w.:-]+)', line)
            if match:
                return match[1]
    raise RuntimeError(f"Snapshot is missing control {name!r}: {snapshot[:500]}")


async def check_interactions(label, proc):
    """Exercise UID input tools and scroll with a focused, fixed input."""
    request_id = 10

    async def tool(name, **arguments):
        nonlocal request_id
        request_id += 1
        return tool_text(
            await mcp_call(
                proc, "tools/call", {"name": name, "arguments": arguments}, request_id
            )
        )

    async def evaluate(function, expected):
        result = await tool("evaluate_script", function=function)
        if expected not in result:
            raise RuntimeError(f"Missing {expected!r} in script result: {result[:500]}")
        return result

    async def capture(stage):
        path = ARTIFACTS / f"{label}-{stage}.png"
        if label == "chrome":
            await tool("take_screenshot", filePath=str(path), format="png")
        else:
            await tool("screenshot_page", saveTo=str(path))
        with path.open("rb") as image:
            if image.read(8) != b"\x89PNG\r\n\x1a\n" or path.stat().st_size <= 24:
                raise RuntimeError(f"MCP did not save a valid PNG: {path}")
        metrics = await tool(
            "evaluate_script",
            function="""() => ({
            scrollY, innerHeight, focused: document.activeElement?.id,
            viewport: { height: visualViewport.height, offsetTop: visualViewport.offsetTop },
            input: document.querySelector('#prompt').getBoundingClientRect().toJSON()
        })""",
        )
        (ARTIFACTS / f"{label}-{stage}-viewport.txt").write_text(metrics)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    await tool(
        "navigate_page", url="data:text/html;charset=utf-8," + quote(INTERACTION_PAGE)
    )
    await evaluate(
        """() => {
        window.awcInitialHeight = visualViewport.height;
        return 'fixture-ready';
    }""",
        "fixture-ready",
    )
    await capture("before-focus")
    snapshot = await tool("take_snapshot")
    click = "click" if label == "chrome" else "click_by_uid"
    fill = "fill" if label == "chrome" else "fill_by_uid"
    await tool(click, uid=snapshot_uid(snapshot, "Smoke prompt"))
    await evaluate(
        """async () => {
        for (let i = 0; i < 30; i++) {
            if (document.activeElement?.id === 'prompt' &&
                visualViewport.height < window.awcInitialHeight - 40) {
                return 'keyboard-visible';
            }
            await new Promise(resolve => setTimeout(resolve, 100));
        }
        throw new Error('Focused input did not open keyboard: ' + JSON.stringify({
            focused: document.activeElement?.id, before: window.awcInitialHeight,
            after: visualViewport.height
        }));
    }""",
        "keyboard-visible",
    )
    print(f"[{label}] input focus and keyboard: PASS")
    snapshot = await tool("take_snapshot")
    await tool(
        fill, uid=snapshot_uid(snapshot, "Smoke prompt"), value="MCP mobile input"
    )
    await evaluate(
        """() => {
        if (document.querySelector('#prompt').value !== 'MCP mobile input') {
            throw new Error('MCP fill did not update the input');
        }
        return 'input-filled';
    }""",
        "input-filled",
    )
    await capture("keyboard-open")

    for direction, position, condition in (
        ("down", "document.scrollingElement.scrollHeight", "scrollY > before + 100"),
        ("up", "0", "before > 100 && scrollY <= 1"),
    ):
        await evaluate(
            """async () => {
            const before = scrollY;
            window.scrollTo({top: POSITION, behavior: 'instant'});
            for (let i = 0; i < 20; i++) {
                if (CONDITION) {
                    if (document.activeElement?.id !== 'prompt' ||
                        visualViewport.height >= window.awcInitialHeight - 40) {
                        throw new Error('Input focus or keyboard lost during scroll');
                    }
                    return 'scroll-DIRECTION-ok';
                }
                await new Promise(resolve => setTimeout(resolve, 100));
            }
            throw new Error('Scroll DIRECTION did not change scrollY: ' + scrollY);
        }""".replace("POSITION", position)
            .replace("CONDITION", condition)
            .replace("DIRECTION", direction),
            f"scroll-{direction}-ok",
        )
        await capture(f"scroll-{direction}")
        print(f"[{label}] scroll {direction} with keyboard: PASS")

    snapshot = await tool("take_snapshot")
    await tool(click, uid=snapshot_uid(snapshot, "Check input"))
    await evaluate(
        """() => {
        if (document.body.dataset.submitted !== 'MCP mobile input') {
            throw new Error('MCP click did not trigger the button handler');
        }
        return 'button-clicked';
    }""",
        "button-clicked",
    )
    print(f"[{label}] fill, button click and screenshots: PASS")


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
        # Keep MCP diagnostics in the Actions log.
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
        await check_interactions(label, proc)
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()


def diagnostics(label, package=None):
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
    commands = {
        "logcat": ("logcat", "-d"),
        "activities": ("shell", "dumpsys", "activity", "activities"),
        "sockets": ("shell", "cat", "/proc/net/unix"),
        "input-method": ("shell", "dumpsys", "input_method"),
    }
    if label == "chrome":
        commands["flags"] = ("shell", "cat", "/data/local/tmp/chrome-command-line")
        commands["package"] = ("shell", "dumpsys", "package", android.CHROME_PACKAGE)
    elif package:
        commands["package"] = ("shell", "dumpsys", "package", package)
        commands["config"] = (
            "shell",
            "cat",
            f"/data/local/tmp/{package}-geckoview-config.yaml",
        )
    for name, args in commands.items():
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
        # The AVD has a hardware keyboard; still show the Android soft keyboard
        # so focus/scroll checks exercise the mobile viewport behavior.
        android.adb(
            "shell", "settings", "put", "secure", "show_ime_with_hard_keyboard", "1"
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
                    # Retain MCP diagnostics without verbose protocol traffic.
                    config["args"] += ["--logFile", str(ARTIFACTS / "chrome-mcp.log")]
                    await drive(label, config, "Chrome")
                print(f"[{label}] PASS")
            except Exception as exc:
                failures.append(label)
                print(f"[{label}] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                diagnostics(label, package)
                if label == "fenix" and package:
                    # Release Firefox's focus and memory before testing Chrome.
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
