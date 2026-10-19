"""Android emulator lifecycle and adb helpers.

The SDK, system image and AVD are baked into the image at build time (see the
Dockerfile), so this module only starts the emulator that is already there and
talks to it over adb. Nothing here downloads or creates an AVD.

Adapted from the prior art on jrmuizel's `android` branch, but calling the
``emulator`` and ``adb`` binaries directly rather than going through
``mozrunner.devices.android_device``, which expects a mozilla-central checkout
(its ``substs`` dict wants a ``top_srcdir``) that does not exist in the
container.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Self

logger = logging.getLogger("autowebcompat-repro")

# Set by the Dockerfile; the defaults keep the module importable on a dev
# machine with a stock SDK layout.
SDK_ROOT = Path(os.environ.get("ANDROID_SDK_ROOT", "/opt/android-sdk"))
AVD_NAME = os.environ.get("ANDROID_AVD_NAME", "webcompat")

# The emulator only ever has one device attached, but adb still wants to be
# told which one when any ambiguity is possible.
DEVICE_SERIAL = "emulator-5554"

# A cold first boot in CI (software GPU, fresh userdata) is much slower
# than a warm local boot with hardware acceleration.
BOOT_TIMEOUT = 900
BOOT_POLL_INTERVAL = 5
ADB_TIMEOUT = 120

# Chrome for Android publishes its DevTools endpoint on this abstract unix
# socket; `adb forward` maps it to a host port the Chrome DevTools MCP attaches
# to. See docs/debugging-android.md in chrome-devtools-mcp.
CHROME_DEVTOOLS_SOCKET = "localabstract:chrome_devtools_remote"
CHROME_DEVTOOLS_PORT = 9222
CHROME_PACKAGE = "com.android.chrome"
# Populated by the Dockerfile from the Play Store system image.
CHROME_APK_DIR = Path(os.environ.get("CHROME_APK_DIR", "/opt/chrome-apks"))


class AndroidError(Exception):
    """Raised when the emulator or a device operation fails."""


def host_abi() -> str:
    """The Android ABI matching the host architecture.

    The image is built for whatever architecture it runs on, so the system
    image, and therefore the Fenix APK, has to match the host rather than being
    pinned. Production runs x86_64; local arm64 hosts are also supported.
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "arm64-v8a"
    raise AndroidError(f"No Android system image for host architecture {machine}")


def sdk_binary(*parts: str) -> Path:
    binary = SDK_ROOT.joinpath(*parts)
    if not binary.exists():
        raise AndroidError(
            f"{binary} not found; the image should install the SDK "
            "(see the Dockerfile)."
        )
    return binary


def adb(*args: str, check: bool = True, timeout: int = ADB_TIMEOUT) -> str:
    """Run an adb command against the emulator and return its stdout."""
    cmd = [str(sdk_binary("platform-tools", "adb")), "-s", DEVICE_SERIAL, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise AndroidError(
            f"adb {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def getprop(name: str) -> str:
    return adb("shell", "getprop", name, check=False).strip()


def install_apk(apk: Path) -> None:
    """Install an APK, replacing any existing copy of the same package."""
    logger.info("installing %s", apk.name)
    # -r replaces an existing install; -d allows a version downgrade, which
    # matters when the release APK lands after a newer nightly.
    output = adb("install", "-r", "-d", str(apk), timeout=600)
    if "Success" not in output:
        raise AndroidError(f"Installing {apk.name} failed: {output.strip()}")


def launcher_activity(package: str) -> str:
    """Resolve a package's launcher activity, as ``package/.Activity``.

    ``am start`` needs an explicit component: a bare package name does not
    resolve, and the activity differs per app (``org.mozilla.fenix/.App`` for
    Fenix, a different one for Chrome), so ask the device rather than
    hardcoding it.
    """
    output = adb(
        "shell",
        "cmd",
        "package",
        "resolve-activity",
        "--brief",
        "-c",
        "android.intent.category.LAUNCHER",
        package,
    )
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith(f"{package}/"):
            return line
    raise AndroidError(f"No launcher activity found for {package}: {output.strip()}")


def start_app(package: str, url: str | None = None) -> None:
    """Launch an app, optionally at a URL, and wait for it to come up.

    Both browsers need this before their MCP server first drives them:
    geckodriver's initial launch of a not-yet-running Fenix fails with
    ``Resource temporarily unavailable`` (EAGAIN), and the Chrome DevTools MCP
    cannot launch Chrome at all -- it only attaches to a running instance.
    """
    args = ["shell", "am", "start", "-n", launcher_activity(package)]
    if url is not None:
        args += ["-a", "android.intent.action.VIEW", "-d", url]
    adb(*args)

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if adb("shell", "pidof", package, check=False).strip():
            logger.info("%s is running", package)
            return
        time.sleep(1)
    raise AndroidError(f"{package} did not start within 60s")


def install_chrome() -> None:
    """Install Chrome onto the emulator from the Play Store system image.

    The google_apis image ships no browser, and there is no sideloadable Chrome
    or Chromium for Android x86_64 (Chrome for Testing is desktop-only;
    Chromium's Android snapshots are arm-only). So the matching Play Store
    system image is kept in the image purely as a source of Chrome's APKs,
    which are extracted from its system partition and installed here.

    Chrome is split into a base APK plus configuration splits, so it must go on
    with ``install-multiple``. Recent builds also factor their native code into
    a shared TrichromeLibrary APK, which has to be installed first because
    Chrome declares a static dependency on it.
    """
    apks = sorted(CHROME_APK_DIR.glob("*.apk")) if CHROME_APK_DIR.is_dir() else []
    if not apks:
        raise AndroidError(
            f"No Chrome APKs in {CHROME_APK_DIR}; the image should extract them "
            "from the Play Store system image (see the Dockerfile)."
        )

    # TrichromeLibrary is a separate package and a prerequisite, so it cannot be
    # part of the install-multiple set for Chrome itself.
    trichrome = [apk for apk in apks if "trichrome" in apk.name.lower()]
    chrome_apks = [apk for apk in apks if apk not in trichrome]

    for lib in trichrome:
        logger.info("installing %s", lib.name)
        output = adb("install", "-r", "-d", str(lib), timeout=600)
        if "Success" not in output:
            raise AndroidError(f"Installing {lib.name} failed: {output.strip()}")

    logger.info("installing Chrome (%d apks)", len(chrome_apks))
    output = adb(
        "install-multiple",
        "-r",
        "-d",
        *[str(apk) for apk in chrome_apks],
        timeout=900,
    )
    if "Success" not in output:
        raise AndroidError(f"Installing Chrome failed: {output.strip()}")


def forward_chrome_devtools(port: int = CHROME_DEVTOOLS_PORT) -> str:
    """Expose Chrome's on-device DevTools socket on a host port."""
    # Clear stale forwards first so a retry doesn't stack duplicates
    # (as wpt's chrome_android.py does before setting up its own).
    adb("forward", "--remove-all", check=False)
    adb("forward", f"tcp:{port}", CHROME_DEVTOOLS_SOCKET)
    endpoint = f"ws://127.0.0.1:{port}/devtools/browser"
    logger.info("forwarded Chrome DevTools to %s", endpoint)
    return endpoint


class Emulator:
    """A running Android emulator, usable as a context manager."""

    def __init__(self, avd_name: str = AVD_NAME) -> None:
        self.avd_name = avd_name
        self.process: subprocess.Popen | None = None

    def start(self) -> Self:
        emulator = sdk_binary("emulator", "emulator")
        cmd = [
            str(emulator),
            "-avd",
            self.avd_name,
            # No display in the container, and nothing human-visible is needed:
            # the agent drives the device entirely over adb.
            "-no-window",
            "-no-audio",
            "-no-boot-anim",
            # Never read or write a snapshot: each run must start from the
            # AVD's pristine state baked into the image.
            "-no-snapshot",
            "-gpu",
            "swiftshader_indirect",
            # These match what wpt uses to run Fenix on emulators
            # (tools/wpt/android.py): a permissive selinux and test_harness
            # let geckodriver chown the Fenix profile it pushes to the device.
            "-selinux",
            "permissive",
            "-prop",
            "ro.test_harness=true",
            "-memory",
            "4096",
            "-camera-back",
            "none",
            "-camera-front",
            "none",
        ]
        logger.info("starting emulator %s", self.avd_name)
        self.process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.wait_for_boot()
        return self

    def wait_for_boot(self, timeout: int = BOOT_TIMEOUT) -> None:
        """Block until the device reports a completed boot."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise AndroidError(
                    f"emulator exited with {self.process.returncode} before booting"
                )
            waited = int(time.monotonic() - (deadline - timeout))
            if waited and waited % 60 < BOOT_POLL_INTERVAL:
                logger.info(
                    "waiting for boot (%ss): state=%r",
                    waited,
                    adb("get-state", check=False).strip(),
                )
            if getprop("sys.boot_completed") == "1":
                # google_apis images are rootable; geckodriver needs this to
                # chown the profile it pushes into the Fenix data dir.
                adb("root", check=False, timeout=30)
                adb("wait-for-device", check=False)
                logger.info(
                    "emulator booted: Android %s, %s",
                    getprop("ro.build.version.release"),
                    getprop("ro.product.cpu.abi"),
                )
                return
            time.sleep(BOOT_POLL_INTERVAL)
        raise AndroidError(f"emulator did not boot within {timeout}s")

    def stop(self) -> None:
        if self.process is None:
            return
        adb("emu", "kill", check=False, timeout=30)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *args, **kwargs) -> None:
        self.stop()
