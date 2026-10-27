"""Android adapters for the existing reproduction tasks and browser factories."""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import mozdownload

from . import android

logger = logging.getLogger("autowebcompat-repro")
PACKAGES = {"nightly": "org.mozilla.fenix", "stable": "org.mozilla.firefox"}


@dataclass(frozen=True)
class AndroidBrowser:
    browser: str
    channel: str = "stable"

    def __str__(self):
        return f"android:{self.browser}:{self.channel}"

    @classmethod
    def parse(cls, value):
        prefix, browser, channel = value.split(":")
        if prefix != "android" or browser not in {"firefox", "chrome"}:
            raise ValueError(f"Not an Android browser: {value}")
        if channel not in {"nightly", "stable"}:
            raise ValueError(f"No Android channel: {channel}")
        return cls(browser, channel)

    @property
    def package(self):
        if self.browser == "chrome":
            return android.CHROME_PACKAGE
        return PACKAGES[self.channel]

    def install(self):
        if android.adb("shell", "pm", "path", self.package, check=False).strip():
            return
        with tempfile.TemporaryDirectory(prefix="fenix-") as tmp:
            options = (
                {"branch": "mozilla-central"}
                if self.channel == "nightly"
                else {"version": "latest"}
            )
            scraper = mozdownload.FactoryScraper(
                "daily" if self.channel == "nightly" else "release",
                application="fenix",
                platform=f"android-{android.host_abi()}",
                destination=tmp,
                **options,
            )
            android.install_apk(Path(scraper.download()))

    def start(self, *, force=False):
        """Activate a browser, using its normal app profile on the device."""
        if self.browser == "chrome":
            if not force:
                try:
                    with urlopen(
                        f"http://127.0.0.1:{android.CHROME_DEVTOOLS_PORT}/json/version",
                        timeout=2,
                    ) as response:
                        endpoint = json.load(response)["webSocketDebuggerUrl"]
                    android.start_app(self.package)
                    return {"browserWSEndpoint": endpoint}
                except OSError, ValueError, KeyError:
                    pass
            android.prepare_chrome()
            android.start_app(self.package, "about:blank")
            return {"browserWSEndpoint": android.forward_chrome_devtools()}

        self.install()
        if (
            not force
            and android.adb("shell", "pidof", self.package, check=False).strip()
        ):
            android.start_app(self.package)
            return self.connection()
        android.adb("shell", "am", "force-stop", self.package)
        config = {
            "env": {"MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"},
            "args": [
                "--marionette",
                "--remote-debugging-port",
                str(android.FIREFOX_BIDI_PORT),
                "--remote-allow-hosts",
                "localhost,127.0.0.1",
            ],
            "prefs": {"marionette.port": android.FIREFOX_MARIONETTE_PORT},
        }
        destination = f"/data/local/tmp/{self.package}-geckoview-config.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(json.dumps(config))
            android.adb("push", str(path), destination)
        android.adb("shell", "chmod", "644", destination)
        # Official APKs aren't debuggable, but the selected debug app can read
        # GeckoView's automation config. No root or app-private file access.
        android.adb("shell", "am", "set-debug-app", "--persistent", self.package)
        for port in (android.FIREFOX_MARIONETTE_PORT, android.FIREFOX_BIDI_PORT):
            android.adb("forward", f"tcp:{port}", f"tcp:{port}")
        output = android.adb(
            "shell",
            "am",
            "start",
            "-n",
            f"{self.package}/org.mozilla.fenix.IntentReceiverActivity",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            "about:blank",
            "--ez",
            "automationtest",
            "true",
            "--ez",
            "performancetest",
            "true",
        )
        if "Error:" in output or "Error type" in output:
            raise android.AndroidError(output)
        android.wait_for_firefox()
        return self.connection()

    def connection(self):
        return {
            "browserWSEndpoint": f"ws://127.0.0.1:{android.FIREFOX_BIDI_PORT}/session",
            "protocol": "webDriverBiDi",
        }


class AndroidFirefoxBrowsers:
    nightly = AndroidBrowser("firefox", "nightly")
    stable = AndroidBrowser("firefox", "stable")


class AndroidChromeBrowsers:
    stable = AndroidBrowser("chrome")


class AndroidEnvironment:
    def __enter__(self):
        self.emulator = android.Emulator().start()
        return AndroidFirefoxBrowsers(), AndroidChromeBrowsers()

    def __exit__(self, *args):
        self.emulator.stop()
