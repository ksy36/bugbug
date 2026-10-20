# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Checks that do not require an Android device or Docker."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extract_mobile_chrome import export_chrome
from hackbot_agents.autowebcompat_repro import android


class ChromeForwardTests(unittest.TestCase):
    def test_discovers_endpoint_without_removing_firefox_forwards(self):
        endpoint = "ws://127.0.0.1:9222/devtools/browser/session-id"
        response = io.StringIO(
            '{"Browser":"Chrome/160", "webSocketDebuggerUrl":"' + endpoint + '"}'
        )
        with (
            patch.object(android, "adb") as adb,
            patch.object(
                android, "devtools_socket_name", return_value="chrome_devtools_remote"
            ),
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
                android, "devtools_socket_name", return_value="chrome_devtools_remote"
            ),
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
            patch.object(
                android, "devtools_socket_name", return_value="chrome_devtools_remote"
            ),
            patch.object(android, "urlopen", side_effect=OSError("connection refused")),
            patch.object(android.time, "monotonic", side_effect=[0, 0, 61]),
            patch.object(android.time, "sleep"),
        ):
            with self.assertRaisesRegex(android.AndroidError, "connection refused"):
                android.forward_chrome_devtools()


class MobileChromeTests(unittest.TestCase):
    def bundle(self, directory):
        packages = []
        for label, files in [
            ("TrichromeLibrary", ["library.apk"]),
            ("com.android.chrome", ["base.apk", "split-x86.apk"]),
        ]:
            entries = []
            for name in files:
                data = name.encode()
                (directory / name).write_bytes(data)
                entries.append(
                    {"file": name, "sha256": hashlib.sha256(data).hexdigest()}
                )
            packages.append({"package": label, "apks": entries})
        manifest = {
            "abi": "x86_64",
            "api": "34",
            "fingerprint": "donor",
            "packages": packages,
        }
        (directory / "manifest.json").write_text(json.dumps(manifest))

    def test_installs_library_before_chrome_with_all_splits(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.bundle(directory)
            with (
                patch.object(android, "getprop", side_effect=["x86_64", "34"]),
                patch.object(android, "adb", return_value="Success") as adb,
            ):
                android.install_mobile_chrome(directory)
            self.assertEqual(
                adb.call_args_list[:2],
                [
                    unittest.mock.call(
                        "install", "-r", str(directory / "library.apk"), timeout=600
                    ),
                    unittest.mock.call(
                        "install-multiple",
                        "-r",
                        str(directory / "base.apk"),
                        str(directory / "split-x86.apk"),
                        timeout=600,
                    ),
                ],
            )

    def test_rejects_corrupt_bundle_before_installing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.bundle(directory)
            (directory / "split-x86.apk").write_bytes(b"corrupt")
            with (
                patch.object(android, "getprop", side_effect=["x86_64", "34"]),
                patch.object(android, "adb") as adb,
            ):
                with self.assertRaisesRegex(android.AndroidError, "checksum"):
                    android.install_mobile_chrome(directory)
                adb.assert_not_called()

    def test_rejects_wrong_architecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.bundle(directory)
            with (
                patch.object(android, "getprop", return_value="arm64-v8a"),
                patch.object(android, "adb") as adb,
            ):
                with self.assertRaisesRegex(android.AndroidError, "ABI"):
                    android.install_mobile_chrome(directory)
                adb.assert_not_called()

    def test_exports_hidden_library_and_chrome_splits(self):
        chrome = [
            "/data/app/com.android.chrome/Chrome.apk",
            "/data/app/com.android.chrome/split.apk",
        ]
        library = "/data/app/com.google.android.trichromelibrary_567263637/TrichromeLibrary.apk"
        details = (
            "Packages:\n"
            "  Package [com.android.chrome]:\n"
            "    versionName=113.0.5672.136\n"
            "    usesStaticLibraries:\n"
            "      com.google.android.trichromelibrary version:567263637\n"
            "    usesLibraryFiles:\n"
            f"      {library}\n"
            "    timeStamp=2026-10-20\n"
            "Hidden system packages:\n"
            "  Package [com.android.chrome]:\n"
            "    codePath=/product/app/Chrome-Stub\n"
        )
        pulled = []

        def adb(*args, **kwargs):
            if args[:3] == ("shell", "pm", "path"):
                return "\n".join(f"package:{path}" for path in chrome)
            if args[:3] == ("shell", "dumpsys", "package"):
                return details
            if args[0] == "pull":
                pulled.append(args[1])
                Path(args[2]).write_bytes(
                    b"library" if "TrichromeLibrary" in args[1] else args[1].encode()
                )
                return ""
            raise AssertionError(f"Unexpected ADB command: {args}")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with (
                patch.object(android, "adb", side_effect=adb),
                patch.object(android, "getprop", side_effect=["34", "x86_64", "donor"]),
                patch("builtins.print"),
            ):
                export_chrome(directory)
            manifest = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(len(manifest["packages"]), 2)
            self.assertEqual(manifest["packages"][-1]["package"], "com.android.chrome")
            self.assertEqual(len(manifest["packages"][-1]["apks"]), 2)
            self.assertEqual(pulled, [library, *chrome])

    def test_rejects_unresolved_trichrome_before_pulling(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                android,
                "adb",
                side_effect=[
                    "package:/data/app/Chrome.apk\n",
                    "Packages:\n    usesStaticLibraries:\n      com.google.android.trichromelibrary version:123\n",
                ],
            ) as adb:
                with self.assertRaisesRegex(
                    android.AndroidError, "no resolved library"
                ):
                    export_chrome(Path(tmp))
                self.assertEqual(adb.call_count, 2)

    def test_rejects_old_stub_bundle_before_installing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.bundle(directory)
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["packages"][0]["apks"][0]["file"] = "TrichromeLibrary-Stub.apk"
            manifest_path.write_text(json.dumps(manifest))
            with (
                patch.object(android, "getprop", side_effect=["x86_64", "34"]),
                patch.object(android, "adb") as adb,
            ):
                with self.assertRaisesRegex(android.AndroidError, "placeholder"):
                    android.install_mobile_chrome(directory)
                adb.assert_not_called()
