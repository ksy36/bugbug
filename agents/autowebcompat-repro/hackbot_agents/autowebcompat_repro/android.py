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

BOOT_TIMEOUT = 300
BOOT_POLL_INTERVAL = 5
ADB_TIMEOUT = 120

# Chrome for Android publishes its DevTools endpoint on this abstract unix
# socket; `adb forward` maps it to a host port the Chrome DevTools MCP attaches
# to. See docs/debugging-android.md in chrome-devtools-mcp.
CHROME_DEVTOOLS_SOCKET = "localabstract:chrome_devtools_remote"
CHROME_DEVTOOLS_PORT = 9222
CHROME_PACKAGE = "com.android.chrome"


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
            # The playstore system image is a locked production build, so adbd
            # refuses the fchown geckodriver issues when pushing a Fenix
            # profile ("adb error: fchown failed: Operation not permitted").
            # Loosen it: a non-playstore image would avoid this but ships no
            # Chrome, which the cross-check needs.
            "-writable-system",
            "-selinux",
            "permissive",
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
            if getprop("sys.boot_completed") == "1":
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
