"""Firefox for Android (Fenix) downloads and on-device installs.

The Android counterpart to ``browser.py``: where that downloads a desktop
Firefox and unpacks it with mozinstall, this downloads an APK and installs it
onto the emulator with adb. mozdownload knows about Fenix directly, so channel
and version resolution is left to it rather than built by hand -- nightly and
release use different directory layouts on archive.mozilla.org.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Literal

import mozdownload

from .android import host_abi, install_apk

logger = logging.getLogger("autowebcompat-repro")

AndroidChannel = Literal["nightly"] | Literal["release"]

# Nightly and release ship as separate apps, so both can be installed at once
# and selected per-task with the MCP's --android-package.
PACKAGE_NAMES: dict[str, str] = {
    "nightly": "org.mozilla.fenix",
    "release": "org.mozilla.firefox",
}


def download_fenix(channel: AndroidChannel) -> Path:
    """Download the Fenix APK for a channel, matching the host architecture."""
    platform_name = f"android-{host_abi()}"
    destination = tempfile.mkdtemp(prefix=f"fenix-{channel}-", dir=Path.home())

    if channel == "nightly":
        scraper_type = "daily"
        kwargs = {}
    else:
        scraper_type = "release"
        kwargs = {"version": "latest"}

    logger.info("downloading Fenix %s for %s...", channel, platform_name)
    scraper = mozdownload.FactoryScraper(
        scraper_type,
        application="fenix",
        platform=platform_name,
        destination=destination,
        **kwargs,
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
