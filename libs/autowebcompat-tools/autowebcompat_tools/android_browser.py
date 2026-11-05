from __future__ import annotations

import json
import logging
import socket
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import mozdownload

from . import android_emulator

logger = logging.getLogger(__name__)
FIREFOX_MARIONETTE_PORT = 2829
FIREFOX_BIDI_PORT = 9223
CHROME_DEVTOOLS_PORT = 9222
CHROME_PACKAGE = "com.android.chrome"
CHROME_SOCKET = "chrome_devtools_remote"
PACKAGES = {"nightly": "org.mozilla.fenix", "stable": "org.mozilla.firefox"}


def prepare_chrome() -> None:
    """Configure the mobile Chrome preinstalled in the Play Store image."""
    # Select Chrome as the debug app so it reads our startup flags,
    # which skip welcome screens and configure its remote debugging socket
    android_emulator.adb("shell", "am", "set-debug-app", "--persistent", CHROME_PACKAGE)
    android_emulator.adb(
        "shell",
        "echo 'chrome --disable-fre --no-first-run --no-default-browser-check "
        f"--remote-debugging-socket-name={CHROME_SOCKET}' "
        "> /data/local/tmp/chrome-command-line",
    )
    android_emulator.adb("shell", "chmod", "644", "/data/local/tmp/chrome-command-line")
    android_emulator.adb("shell", "am", "force-stop", CHROME_PACKAGE)


def wait_for_firefox(timeout: int = 120) -> None:
    """Wait for the Marionette greeting, without creating a session."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # adb forward accepts the connection even when nothing listens on the
        # device, then closes it, so only a greeting proves Firefox is ready.
        try:
            with socket.create_connection(
                ("127.0.0.1", FIREFOX_MARIONETTE_PORT), timeout=2
            ) as connection:
                greeting = b""
                while b'"applicationType":"gecko"' not in greeting:
                    chunk = connection.recv(256)
                    if not chunk or len(greeting) > 4096:
                        break
                    greeting += chunk
                else:
                    logger.info("Firefox Marionette is ready for MCP attachment")
                    return
        except OSError:
            pass
        time.sleep(1)
    raise android_emulator.AndroidError(
        f"Firefox Marionette did not become ready within {timeout}s"
    )


def chrome_ready(port: int = CHROME_DEVTOOLS_PORT) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            version = json.load(response)
    except OSError, ValueError:
        return False
    if not version.get("webSocketDebuggerUrl"):
        return False
    logger.info("%s DevTools ready on port %s", version.get("Browser"), port)
    return True


def forward_chrome_devtools(port: int = CHROME_DEVTOOLS_PORT) -> None:
    """Expose Chrome's on-device DevTools socket on a host port."""
    android_emulator.adb("forward", f"tcp:{port}", f"localabstract:{CHROME_SOCKET}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if chrome_ready(port):
            return
        time.sleep(1)
    raise android_emulator.AndroidError(
        f"Chrome DevTools on port {port} did not become ready"
    )


@dataclass(frozen=True)
class AndroidBrowser:
    browser: str
    channel: str = "stable"

    def __str__(self):
        return f"android:{self.browser}:{self.channel}"

    @classmethod
    def parse(cls, value):
        _, browser, channel = value.split(":")
        return cls(browser, channel)

    @property
    def package(self):
        if self.browser == "chrome":
            return CHROME_PACKAGE
        return PACKAGES[self.channel]

    def install(self):
        if android_emulator.adb(
            "shell", "pm", "path", self.package, check=False
        ).strip():
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
                platform="android-x86_64",
                destination=tmp,
                **options,
            )
            android_emulator.install_apk(Path(scraper.download()))

    def activate(self):
        """Bring a browser forward, restoring automation if it stopped working."""
        if self.browser == "chrome":
            ready = chrome_ready()
        else:
            ready = bool(
                android_emulator.adb(
                    "shell", "pidof", self.package, check=False
                ).strip()
            )
        if not ready:
            # The other browser may have replaced Android's global debug-app
            # selection. Restore it before relaunching so automation flags load.
            self.start()
            return
        android_emulator.start_app(self.package)

    def start(self):
        """Initialize a browser, using its normal app profile on the device."""
        if self.browser == "chrome":
            prepare_chrome()
            android_emulator.start_app(self.package, "about:blank")
            forward_chrome_devtools()
            return

        self.install()
        # Nightly and release use the same device ports. Stopping only the
        # requested package lets a previous channel answer the new connection.
        for package in PACKAGES.values():
            android_emulator.adb("shell", "am", "force-stop", package)
        # force-stop can return before the process exits; a survivor would
        # answer on the shared ports and the run would test the wrong channel.
        deadline = time.monotonic() + 10
        while running := [
            p
            for p in PACKAGES.values()
            if android_emulator.adb("shell", "pidof", p, check=False).strip()
        ]:
            if time.monotonic() > deadline:
                raise android_emulator.AndroidError(
                    f"Firefox packages did not stop: {running}"
                )
            time.sleep(0.2)
        logger.info("Starting Firefox %s: package=%s", self.channel, self.package)
        config = {
            "env": {"MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"},
            "args": [
                "--marionette",
                "--remote-debugging-port",
                str(FIREFOX_BIDI_PORT),
                "--remote-allow-hosts",
                "localhost,127.0.0.1",
            ],
            "prefs": {"marionette.port": FIREFOX_MARIONETTE_PORT},
        }

        destination = f"/data/local/tmp/{self.package}-geckoview-config.yaml"
        android_emulator.adb("shell", f"echo '{json.dumps(config)}' > {destination}")
        android_emulator.adb("shell", "chmod", "644", destination)
        android_emulator.adb(
            "shell", "am", "set-debug-app", "--persistent", self.package
        )
        for port in (FIREFOX_MARIONETTE_PORT, FIREFOX_BIDI_PORT):
            android_emulator.adb("forward", f"tcp:{port}", f"tcp:{port}")
        android_emulator.am_start(
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
        wait_for_firefox()

    @property
    def endpoint(self):
        if self.browser == "chrome":
            return f"http://127.0.0.1:{CHROME_DEVTOOLS_PORT}"
        return f"ws://127.0.0.1:{FIREFOX_BIDI_PORT}/session"


class AndroidFirefoxBrowsers:
    nightly = AndroidBrowser("firefox", "nightly")
    stable = AndroidBrowser("firefox", "stable")


class AndroidChromeBrowsers:
    stable = AndroidBrowser("chrome")


@contextmanager
def AndroidEnvironment():
    with android_emulator.emulator():
        yield AndroidFirefoxBrowsers(), AndroidChromeBrowsers()
