# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Checks that do not require an Android device or Docker."""

import io
import unittest
from unittest.mock import patch

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
