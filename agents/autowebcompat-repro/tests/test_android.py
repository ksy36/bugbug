# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Checks that do not require an Android device or Docker."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import smoke_android
from hackbot_agents.autowebcompat_repro import android
from hackbot_agents.autowebcompat_repro.android_browser import nightly_source


class FenixSourceTests(unittest.TestCase):
    def test_default_is_official_debug_apk_for_emulator_architecture(self):
        with (
            patch.dict(
                os.environ,
                {"ANDROID_FIREFOX_APK_URL": "", "ANDROID_FIREFOX_PACKAGE": ""},
            ),
            patch(
                "hackbot_agents.autowebcompat_repro.android_browser.host_abi",
                return_value="x86_64",
            ),
        ):
            url, package = nightly_source()
        self.assertEqual(
            url,
            "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/"
            "gecko.v2.mozilla-central.latest.mobile.fenix-debug/artifacts/public/build/target.x86_64.apk",
        )
        self.assertEqual(package, "org.mozilla.fenix.debug")

    def test_custom_apk_and_package_are_preserved(self):
        with patch.dict(
            os.environ,
            {
                "ANDROID_FIREFOX_APK_URL": "https://example.com/custom.apk",
                "ANDROID_FIREFOX_PACKAGE": "org.example.firefox.debug",
            },
        ):
            self.assertEqual(
                nightly_source(),
                ("https://example.com/custom.apk", "org.example.firefox.debug"),
            )


class FirefoxAttachTests(unittest.TestCase):
    def test_launches_once_with_private_profile_and_forwards_both_protocols(self):
        configs = []

        def adb_result(*args, **kwargs):
            if args[0] == "push":
                configs.append(json.loads(Path(args[1]).read_text()))
            return "Starting: Intent"

        with (
            patch.object(android, "require_debuggable"),
            patch.object(android, "adb", side_effect=adb_result) as adb,
            patch.object(android, "wait_for_firefox") as ready,
        ):
            android.prepare_firefox("org.mozilla.fenix.debug")
        calls = [call.args for call in adb.call_args_list]
        launches = [
            args for args in calls if args[0] == "shell" and "am start" in args[1]
        ]
        self.assertEqual(len(launches), 1)
        self.assertNotIn("-S", launches[0][1])
        self.assertIn("--ez automationtest true", launches[0][1])
        self.assertIn(("forward", "tcp:2829", "tcp:2829"), calls)
        self.assertIn(("forward", "tcp:9223", "tcp:9223"), calls)
        self.assertEqual(configs[0]["prefs"]["marionette.port"], 2829)
        self.assertIn(
            "/data/data/org.mozilla.fenix.debug/awc-profile", configs[0]["args"]
        )
        self.assertNotIn(("shell", "pm", "clear", "org.mozilla.fenix.debug"), calls)
        ready.assert_called_once_with()

    def test_waits_for_protocol_greeting_after_initial_connection_failure(self):
        greeting = json.dumps(
            {"applicationType": "gecko", "marionetteProtocol": 3}
        ).encode()
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.makefile.return_value = io.BytesIO(
            str(len(greeting)).encode() + b":" + greeting
        )
        with (
            patch.object(
                android.socket,
                "create_connection",
                side_effect=[ConnectionRefusedError(), connection],
            ) as connect,
            patch.object(android.time, "sleep"),
        ):
            android.wait_for_firefox()
        self.assertEqual(connect.call_count, 2)

    def test_readiness_failure_has_bounded_timeout(self):
        with (
            patch.object(
                android.socket,
                "create_connection",
                side_effect=ConnectionRefusedError("not ready"),
            ),
            patch.object(android.time, "monotonic", side_effect=[0, 0, 121]),
            patch.object(android.time, "sleep"),
        ):
            with self.assertRaisesRegex(android.AndroidError, "within 120s: not ready"):
                android.wait_for_firefox()


class LauncherTests(unittest.TestCase):
    def test_queries_main_launcher_with_explicit_package(self):
        package = "org.mozilla.fenix.debug"
        component = f"{package}/.App"
        with patch.object(android, "adb", return_value=component + "\n") as adb:
            self.assertEqual(android.launcher_activity(package), component)
        adb.assert_called_once_with(
            "shell",
            "cmd",
            "package",
            "query-activities",
            "--components",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            package,
        )

    def test_never_selects_android_resolver_as_browser(self):
        with patch.object(
            android,
            "adb",
            return_value="android/com.android.internal.app.ResolverActivity\n",
        ):
            with self.assertRaisesRegex(
                android.AndroidError, "No launcher activity found"
            ):
                android.launcher_activity("org.mozilla.fenix.debug")

    def test_starts_fenix_using_main_launcher_intent(self):
        package = "org.mozilla.fenix.debug"
        with (
            patch.object(android, "launcher_activity", return_value=f"{package}/.App"),
            patch.object(
                android, "adb", side_effect=["Starting: Intent", "1234\n"]
            ) as adb,
        ):
            android.start_app(package)
        self.assertEqual(
            adb.call_args_list[0],
            unittest.mock.call(
                "shell",
                "am",
                "start",
                "-n",
                f"{package}/.App",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
            ),
        )


class EmulatorBootTests(unittest.TestCase):
    def test_locked_image_boot_does_not_request_root(self):
        with (
            patch.object(
                android,
                "adb",
                return_value="List of devices attached\nemulator-5554 device\n",
            ) as adb,
            patch.object(android, "getprop", side_effect=["1", "16", "x86_64"]),
        ):
            android.Emulator().wait_for_boot()
        self.assertEqual(
            adb.call_args_list, [unittest.mock.call("devices", "-l", timeout=10)]
        )

    def test_unauthorized_device_fails_before_boot_timeout(self):
        clock = [0]

        def sleep(seconds):
            clock[0] += seconds

        with (
            patch.object(
                android,
                "adb",
                return_value="List of devices attached\nemulator-5554 unauthorized\n",
            ),
            patch.object(android, "getprop") as getprop,
            patch.object(android.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(android.time, "sleep", side_effect=sleep),
        ):
            with self.assertRaisesRegex(
                android.AndroidError, "ADB device unauthorized"
            ):
                android.Emulator().wait_for_boot()
            getprop.assert_not_called()
        self.assertEqual(clock[0], android.ADB_AUTH_TIMEOUT)

    def test_missing_device_fails_before_boot_timeout(self):
        clock = [0]

        def sleep(seconds):
            clock[0] += seconds

        with (
            patch.object(android, "adb", return_value="List of devices attached\n"),
            patch.object(android.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(android.time, "sleep", side_effect=sleep),
        ):
            with self.assertRaisesRegex(android.AndroidError, "did not connect to ADB"):
                android.Emulator().wait_for_boot()
        self.assertEqual(clock[0], android.ADB_CONNECT_TIMEOUT)

    def test_failed_start_terminates_emulator(self):
        process = Mock()
        process.poll.return_value = None
        with (
            patch.object(android, "prepare_adb"),
            patch.object(android, "sdk_binary", return_value=Path("/sdk/emulator")),
            patch.object(android.subprocess, "Popen", return_value=process),
            patch.object(
                android.Emulator,
                "wait_for_boot",
                side_effect=android.AndroidError("unauthorized"),
            ),
        ):
            emulator = android.Emulator()
            with self.assertRaisesRegex(android.AndroidError, "unauthorized"):
                emulator.start()
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertIsNone(emulator.process)

    def test_creates_authentication_key_before_starting_adb(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "android" / "adbkey"
            with (
                patch.dict(os.environ, {"ADB_VENDOR_KEYS": str(key)}),
                patch.object(android, "sdk_binary", return_value=Path("/sdk/adb")),
                patch.object(android.subprocess, "run", side_effect=run),
            ):
                android.prepare_adb()
            self.assertEqual(
                calls, [["/sdk/adb", "keygen", str(key)], ["/sdk/adb", "start-server"]]
            )


class ChromeSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_page_before_navigation(self):
        proc = SimpleNamespace(
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            returncode=None,
            terminate=Mock(),
            wait=AsyncMock(),
        )

        def result(text):
            return {"result": {"content": [{"type": "text", "text": text}]}}

        call = AsyncMock(
            side_effect=[
                {"result": {}},
                result("Created page"),
                result("Navigated"),
                result("uid=1 Example Domain"),
                result(f"Android Mobile Chrome {smoke_android.URL}"),
            ]
        )
        with (
            patch.object(
                smoke_android.asyncio, "create_subprocess_exec", return_value=proc
            ),
            patch.object(smoke_android, "mcp_call", call),
            patch("builtins.print"),
        ):
            await smoke_android.drive(
                "chrome", {"command": "mcp", "args": []}, "Chrome"
            )
        names = [c.args[2]["name"] for c in call.await_args_list[1:]]
        self.assertEqual(
            names, ["new_page", "navigate_page", "take_snapshot", "evaluate_script"]
        )
        proc.terminate.assert_called_once()

    async def test_stops_when_tab_creation_fails(self):
        proc = SimpleNamespace(
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            returncode=None,
            terminate=Mock(),
            wait=AsyncMock(),
        )
        call = AsyncMock(
            side_effect=[
                {"result": {}},
                {
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "tab creation failed"}],
                    }
                },
            ]
        )
        with (
            patch.object(
                smoke_android.asyncio, "create_subprocess_exec", return_value=proc
            ),
            patch.object(smoke_android, "mcp_call", call),
        ):
            with self.assertRaisesRegex(RuntimeError, "tab creation failed"):
                await smoke_android.drive(
                    "chrome", {"command": "mcp", "args": []}, "Chrome"
                )
        self.assertEqual(call.await_count, 2)
        proc.terminate.assert_called_once()


class ChromeForwardTests(unittest.TestCase):
    def test_discovers_endpoint_without_removing_firefox_forwards(self):
        endpoint = "ws://127.0.0.1:9222/devtools/browser/session-id"
        response = io.StringIO(
            '{"Browser":"Chrome/160", "webSocketDebuggerUrl":"' + endpoint + '"}'
        )
        with (
            patch.object(android, "adb") as adb,
            patch.object(android, "urlopen", return_value=response),
        ):
            self.assertEqual(android.forward_chrome_devtools(), endpoint)
        self.assertEqual(
            adb.call_args_list,
            [
                unittest.mock.call("forward", "--remove", "tcp:9222", check=False),
                unittest.mock.call(
                    "forward", "tcp:9222", "localabstract:chrome_devtools_remote"
                ),
            ],
        )

    def test_retries_until_cdp_is_ready(self):
        response = io.StringIO(
            '{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/ready"}'
        )
        with (
            patch.object(android, "adb"),
            patch.object(
                android, "urlopen", side_effect=[OSError("not ready"), response]
            ) as connect,
            patch.object(android.time, "sleep"),
        ):
            self.assertTrue(android.forward_chrome_devtools().endswith("/ready"))
            self.assertEqual(connect.call_count, 2)

    def test_reports_readiness_timeout(self):
        with (
            patch.object(android, "adb"),
            patch.object(android, "urlopen", side_effect=OSError("connection refused")),
            patch.object(android.time, "monotonic", side_effect=[0, 0, 61]),
            patch.object(android.time, "sleep"),
        ):
            with self.assertRaisesRegex(android.AndroidError, "connection refused"):
                android.forward_chrome_devtools()


class LockedImageTests(unittest.TestCase):
    def test_chrome_uses_preinstalled_package_without_installing_apks(self):
        def response(*args, **kwargs):
            if args[:3] == ("shell", "pm", "path"):
                return "package:/data/app/com.android.chrome/base.apk\n"
            if args[:3] == ("shell", "dumpsys", "package"):
                return "versionName=140.0\n"
            return ""

        with patch.object(android, "adb", side_effect=response) as adb:
            android.prepare_chrome()
        calls = [call.args for call in adb.call_args_list]
        self.assertIn(
            ("shell", "am", "set-debug-app", "--persistent", "com.android.chrome"),
            calls,
        )
        self.assertFalse(
            any(args[0].startswith("install") or args[0] == "root" for args in calls)
        )

    def test_firefox_preflight_accepts_app_user(self):
        with patch.object(
            android, "adb", return_value="uid=10123(u0_a123) gid=10123\n"
        ):
            android.require_debuggable("org.mozilla.fenix.debug")

    def test_firefox_preflight_reports_nondebuggable_apk(self):
        with patch.object(
            android, "adb", side_effect=android.AndroidError("package not debuggable")
        ):
            with self.assertRaisesRegex(
                android.AndroidError, "requires a debuggable APK"
            ):
                android.require_debuggable("org.mozilla.fenix")
