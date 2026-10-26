# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Agent integration checks, run when the workspace dependencies are installed."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


@unittest.skipUnless(
    importlib.util.find_spec("claude_agent_sdk")
    and importlib.util.find_spec("hackbot_runtime"),
    "requires agent workspace dependencies",
)
class AndroidAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_chrome_starts_only_when_cross_check_tool_is_called(self):
        from mcp import types
        from hackbot_agents.autowebcompat_repro import agent, android

        events = []
        results = SimpleNamespace(
            initial_repro=None,
            reproduced=False,
            chrome_reproduced=None,
            plan_result=SimpleNamespace(affects_channels=[]),
            set_result=Mock(),
        )

        async def investigate(task):
            self.assertEqual(events, ["firefox ready"])
            server = task.mcp_servers["android-browser"]["instance"]
            await server.request_handlers[types.ListToolsRequest](
                types.ListToolsRequest(method="tools/list")
            )
            self.assertEqual(events, ["firefox ready"])
            response = await server.request_handlers[types.CallToolRequest](
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(name="start_chrome", arguments={}),
                )
            )
            self.assertFalse(response.root.isError)
            self.assertIn("DevTools ready", response.root.content[0].text)
            return "test result"

        def start_chrome():
            events.append("chrome requested")
            return "ws://127.0.0.1:9222/devtools/browser"

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(android, "Emulator"),
            patch.object(android, "prepare_chrome", side_effect=AssertionError("eager Chrome setup")),
            patch.object(android, "start_chrome", side_effect=start_chrome),
            patch.object(android, "prepare_firefox", side_effect=lambda package: events.append("firefox ready")),
            patch.object(android, "save_diagnostics"),
            patch.object(agent.AndroidBrowsers, "package", return_value="org.mozilla.fenix.debug"),
            patch.object(agent, "build_android_chrome_devtools_server", return_value={"command": "chrome-mcp", "args": []}),
            patch.object(agent, "build_android_firefox_devtools_server", return_value={"command": "firefox-mcp", "args": []}),
            patch.object(agent.Task, "run", new=investigate),
        ):
            await agent.run_android_repro(
                agent.TaskConfig(artifacts_dir=Path(tmp)),
                agent.RunTracker(),
                agent.BugDataInput("Android-only test report"),
                {},
                results,
                Path(tmp),
            )
        self.assertEqual(events, ["firefox ready", "chrome requested"])
        results.set_result.assert_called_once_with(
            agent.FirefoxChannel.android_nightly, None, "test result"
        )

    async def test_startup_failure_is_reported_as_a_tool_error(self):
        from mcp import types
        from hackbot_agents.autowebcompat_repro import android
        from hackbot_agents.autowebcompat_repro.mcp_servers import build_android_browser_server

        server = build_android_browser_server()["instance"]
        with patch.object(android, "start_chrome", side_effect=android.AndroidError("no DevTools socket")):
            response = await server.request_handlers[types.CallToolRequest](
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(name="start_chrome", arguments={}),
                )
            )
        self.assertTrue(response.root.isError)
        self.assertIn("no DevTools socket", response.root.content[0].text)
