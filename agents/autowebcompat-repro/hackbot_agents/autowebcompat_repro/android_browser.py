"""Firefox for Android (Fenix) downloads and on-device installs.

The Android counterpart to ``browser.py``: where that downloads a desktop
Firefox and unpacks it with mozinstall, this downloads an APK and installs it
onto the emulator with adb. The Nightly test slot uses Mozilla CI's debuggable
Fenix APK so automation can create an app-private profile on a locked device.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from .android import host_abi, install_apk

logger = logging.getLogger("autowebcompat-repro")

AndroidChannel = Literal["nightly", "release"]

# Both channels use Mozilla CI's debuggable builds, which automation needs to
# create an app-private profile via run-as on a locked device. They install
# under the same package name, so only one channel can be present at a time --
# fine because the reproduction passes run sequentially.
DEBUG_PACKAGE = "org.mozilla.fenix.debug"

# Taskcluster index branch per channel. Android has no ESR channel.
CHANNEL_BRANCHES: dict[str, str] = {
    "nightly": "mozilla-central",
    "release": "mozilla-release",
}


def channel_source(channel: AndroidChannel) -> tuple[str, str]:
    """Resolve the debuggable Fenix APK URL and its installed package name.

    ``ANDROID_FIREFOX_APK_URL``/``ANDROID_FIREFOX_PACKAGE`` override the
    nightly slot, for testing against a local or custom build.
    """
    if channel == "nightly" and os.environ.get("ANDROID_FIREFOX_APK_URL"):
        return (
            os.environ["ANDROID_FIREFOX_APK_URL"],
            os.environ.get("ANDROID_FIREFOX_PACKAGE") or DEBUG_PACKAGE,
        )
    url = (
        "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/"
        f"gecko.v2.{CHANNEL_BRANCHES[channel]}.latest.mobile.fenix-debug/"
        f"artifacts/public/build/target.{host_abi()}.apk"
    )
    return url, DEBUG_PACKAGE


def download_fenix(channel: AndroidChannel, destination: Path) -> Path:
    """Download the Fenix APK for a channel, matching the host architecture."""
    platform_name = f"android-{host_abi()}"

    import requests

    apk_url, _ = channel_source(channel)
    apk = destination / "firefox-test.apk"
    logger.info(
        "downloading debuggable Fenix %s for %s from %s",
        channel,
        platform_name,
        apk_url,
    )
    with requests.get(apk_url, stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        with apk.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                output.write(chunk)
    return apk


def install_fenix(channel: AndroidChannel) -> str:
    """Download and install a Fenix channel; return its package name."""
    with tempfile.TemporaryDirectory(prefix=f"fenix-{channel}-") as destination:
        apk = download_fenix(channel, Path(destination))
        install_apk(apk)

    _, package = channel_source(channel)
    logger.info("installed Fenix %s as %s", channel, package)
    return package


class AndroidBrowsers:
    """Installs Fenix channels on demand, one at a time.

    Mirrors ``FirefoxBrowsers``, but returns the on-device package name rather
    than a path to a binary. Unlike desktop, channels are not independent: the
    debuggable builds all install under one package, so installing a channel
    replaces the previous one. Only the most recently installed channel is on
    the device, which suits the sequential reproduction passes.
    """

    def __init__(self) -> None:
        self._installed: AndroidChannel | None = None
        self._package: str | None = None

    def package(self, channel: AndroidChannel) -> str:
        if self._installed != channel or self._package is None:
            self._package = install_fenix(channel)
            self._installed = channel
        return self._package

    @property
    def nightly(self) -> str:
        return self.package("nightly")

    @property
    def release(self) -> str:
        return self.package("release")
