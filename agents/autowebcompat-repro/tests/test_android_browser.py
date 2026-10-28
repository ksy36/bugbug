import os
import importlib.util
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hackbot_agents.autowebcompat_repro import android
from hackbot_agents.autowebcompat_repro.android_browser import AndroidBrowser


class FirefoxChannelTests(unittest.TestCase):
    def setUp(self):
        self.nightly = "org.mozilla.fenix"
        self.stable = "org.mozilla.firefox"
        self.running = {self.nightly, "com.android.chrome"}
        self.sticky = set()
        self.launched = []
        self.connected = []

        def adb(*args, **kwargs):
            if args[:3] == ("shell", "am", "force-stop"):
                if args[3] not in self.sticky:
                    self.running.discard(args[3])
            elif args[:2] == ("shell", "pidof"):
                return "1234" if args[2] in self.running else ""
            elif args[:3] == ("shell", "dumpsys", "package"):
                return "    versionName=" + ("159.0" if args[3] == self.stable else "161.0a1")
            elif args[:3] == ("shell", "am", "start"):
                package = args[4].split("/")[0]
                self.launched.append(package)
                self.running.add(package)
            return ""

        def connect():
            # Both channels share ports. Any surviving previous process would
            # make the connection ambiguous, as happened in the release run.
            listeners = self.running & {self.nightly, self.stable}
            self.assertEqual(len(listeners), 1)
            self.connected.append(next(iter(listeners)))

        self.enterContext(patch.dict(os.environ, {"ANDROID_FIREFOX_PACKAGE": "org.mozilla.fenix.debug"}))
        self.enterContext(patch.object(AndroidBrowser, "install"))
        self.enterContext(patch.object(android, "adb", side_effect=adb))
        self.enterContext(patch.object(android, "wait_for_firefox", side_effect=connect))

    def test_nightly_to_stable_then_back_on_same_device(self):
        AndroidBrowser("firefox", "stable").start(force=True)
        AndroidBrowser("firefox", "nightly").start(force=True)
        self.assertEqual(self.connected, [self.stable, self.nightly])
        self.assertIn("com.android.chrome", self.running)

    def test_custom_firefox_package_releases_ports(self):
        self.running.add("org.mozilla.fenix.debug")
        AndroidBrowser("firefox", "stable").start(force=True)
        self.assertNotIn("org.mozilla.fenix.debug", self.running)
        self.assertEqual(self.connected, [self.stable])

    def test_reuse_keeps_selected_package_but_stops_other_channel(self):
        self.running.add(self.stable)
        with patch.object(android, "start_app") as activate:
            AndroidBrowser("firefox", "stable").start()
        self.assertIn(self.stable, self.running)
        self.assertNotIn(self.nightly, self.running)
        activate.assert_called_once_with(self.stable)
        self.assertEqual(self.launched, [])

    def test_stop_failure_prevents_connecting_to_previous_channel(self):
        self.sticky.add(self.nightly)
        with patch.object(android.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(android.AndroidError, self.nightly):
                AndroidBrowser("firefox", "stable").start(force=True)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.connected, [])

    def test_reports_installed_release_version(self):
        self.assertEqual(AndroidBrowser("firefox", "stable").version(), "159.0")


class FirefoxShutdownTests(unittest.TestCase):
    def test_waits_for_process_to_exit(self):
        def adb(*args, **kwargs):
            return next(pids) if args[:2] == ("shell", "pidof") else ""

        pids = iter(["1234", "1234", ""])
        with patch.object(android, "adb", side_effect=adb), patch.object(android.time, "sleep") as sleep:
            android.stop_firefox_packages({"org.mozilla.fenix"})
        self.assertEqual(sleep.call_count, 2)

    def test_missing_version_fails_explicitly(self):
        with patch.object(android, "adb", return_value=""):
            with self.assertRaisesRegex(android.AndroidError, "Cannot determine Firefox version"):
                AndroidBrowser("firefox", "stable").version()


class SmokeVersionTests(unittest.IsolatedAsyncioTestCase):
    async def check_version(self, reported_major):
        path = Path(__file__).resolve().parents[1] / "android_smoke.py"
        spec = importlib.util.spec_from_file_location("android_smoke_test", path)
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)

        @asynccontextmanager
        async def connect(config):
            yield object()

        ua = f'Mozilla/5.0 (Android 16; Mobile; rv:{reported_major}.0) Firefox/{reported_major}.0'
        with (
            patch.object(smoke, "build_android_firefox_devtools_server", return_value={}),
            patch.object(smoke, "connect", connect),
            patch.object(smoke, "call", new_callable=AsyncMock, return_value=ua),
            patch.object(AndroidBrowser, "version", return_value="159.0"),
        ):
            await smoke.firefox("stable")

    async def test_rejects_nightly_connection_during_release_check(self):
        with self.assertRaisesRegex(RuntimeError, "Requested org.mozilla.firefox 159.0"):
            await self.check_version(161)

    async def test_accepts_matching_release_connection(self):
        await self.check_version(159)
