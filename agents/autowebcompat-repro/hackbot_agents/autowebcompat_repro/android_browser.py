"""Firefox for Android (Fenix) downloads and on-device installs.

The Android counterpart to ``browser.py``: where that downloads a desktop
Firefox and unpacks it with mozinstall, this downloads an APK and installs it
onto the emulator with adb. The Nightly test slot uses Mozilla CI's debuggable
Fenix APK so geckodriver can use app-private storage on a locked device.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from .android import host_abi, install_apk, require_debuggable

logger = logging.getLogger("autowebcompat-repro")

AndroidChannel = Literal["nightly"] | Literal["release"]

# The CI debug build and release use separate package names.
PACKAGE_NAMES: dict[str, str] = {
    "nightly": "org.mozilla.fenix.debug",
    "release": "org.mozilla.firefox",
}


def nightly_source() -> tuple[str, str]:
    """Resolve the debuggable Fenix APK and its installed package name."""
    url = os.environ.get("ANDROID_FIREFOX_APK_URL") or (
        "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/"
        "gecko.v2.mozilla-central.latest.mobile.fenix-debug/"
        f"artifacts/public/build/target.{host_abi()}.apk"
    )
    package = os.environ.get("ANDROID_FIREFOX_PACKAGE") or PACKAGE_NAMES["nightly"]
    return url, package


def download_fenix(channel: AndroidChannel) -> Path:
    """Download the Fenix APK for a channel, matching the host architecture."""
    platform_name = f"android-{host_abi()}"
    destination = tempfile.mkdtemp(prefix=f"fenix-{channel}-", dir=Path.home())

    if channel == "nightly":
        import requests

        apk_url, _ = nightly_source()
        apk = Path(destination) / "firefox-test.apk"
        logger.info(
            "downloading debuggable Fenix APK for %s from %s", platform_name, apk_url
        )
        with requests.get(apk_url, stream=True, timeout=(30, 300)) as response:
            response.raise_for_status()
            with apk.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    output.write(chunk)
        return apk

    import mozdownload

    logger.info("downloading Fenix %s for %s...", channel, platform_name)
    scraper = mozdownload.FactoryScraper(
        "release",
        application="fenix",
        platform=platform_name,
        destination=destination,
        version="latest",
    )
    apk = Path(scraper.download())
    logger.info("downloaded %s", apk.name)
    return apk


def install_fenix(channel: AndroidChannel) -> str:
    """Download and install a Fenix channel; return its package name."""
    apk = download_fenix(channel)
    install_apk(apk)
    # The APK is large and only needed for the install itself.
    apk.unlink(missing_ok=True)

    package = PACKAGE_NAMES[channel]
    if channel == "nightly":
        _, package = nightly_source()
    require_debuggable(package)
    logger.info("installed Fenix %s as %s", channel, package)
    return package


class AndroidBrowsers:
    """Installs Fenix channels on demand, once each.

    Mirrors ``FirefoxBrowsers``, but the value cached per channel is the
    on-device package name rather than a path to a binary.
    """

    def __init__(self) -> None:
        self._packages: dict[str, str] = {}

    def package(self, channel: AndroidChannel) -> str:
        if channel not in self._packages:
            self._packages[channel] = install_fenix(channel)
        return self._packages[channel]

    @property
    def nightly(self) -> str:
        return self.package("nightly")

    @property
    def release(self) -> str:
        return self.package("release")
