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

import json
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Self
from urllib.request import urlopen

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
BOOT_TIMEOUT = 300
BOOT_POLL_INTERVAL = 5
ADB_TIMEOUT = 120
ADB_CONNECT_TIMEOUT = 120
ADB_AUTH_TIMEOUT = 30

# Chrome for Android publishes its DevTools endpoint on this abstract unix
# socket; `adb forward` maps it to a host port the Chrome DevTools MCP attaches
# to. See docs/debugging-android.md in chrome-devtools-mcp.
CHROME_DEVTOOLS_PORT = 9222
CHROME_PACKAGE = "com.android.chrome"
CHROME_SOCKET = "chrome_devtools_remote"


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


def prepare_adb() -> None:
    """Create the shared authentication key and start ADB before the emulator."""
    binary = str(sdk_binary("platform-tools", "adb"))
    # Docker sets this to the same .android directory used by the emulator.
    # Generate per-container keys at runtime; never bake private keys into an
    # image. Starting ADB first also makes its default user key available to
    # the emulator before it injects trusted public keys into the guest.
    key_name = os.environ.get("ADB_VENDOR_KEYS")
    if key_name:
        key = Path(key_name)
        key.parent.mkdir(parents=True, exist_ok=True)
        if not key.exists():
            proc = subprocess.run(
                [binary, "keygen", str(key)], capture_output=True, text=True, timeout=30
            )
            if proc.returncode:
                raise AndroidError(f"ADB key generation failed: {proc.stderr.strip()}")
    proc = subprocess.run(
        [binary, "start-server"], capture_output=True, text=True, timeout=30
    )
    if proc.returncode:
        raise AndroidError(f"ADB server failed to start: {proc.stderr.strip()}")


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
        "query-activities",
        "--components",
        "-a",
        "android.intent.action.MAIN",
        "-c",
        "android.intent.category.LAUNCHER",
        "-p",
        package,
    )
    for line in output.strip().splitlines():
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
    else:
        args += [
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
        ]
    output = adb(*args)
    if "Error:" in output or "Error type" in output:
        raise AndroidError(f"Launching {package} failed: {output.strip()}")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if adb("shell", "pidof", package, check=False).strip():
            logger.info("%s is running", package)
            return
        time.sleep(1)
    raise AndroidError(f"{package} did not start within 60s")


def prepare_chrome() -> None:
    """Configure the mobile Chrome preinstalled in the Play Store image."""
    paths = adb("shell", "pm", "path", CHROME_PACKAGE)
    if not any(line.startswith("package:") for line in paths.splitlines()):
        raise AndroidError(
            f"{CHROME_PACKAGE} is missing; use a google_apis_playstore image"
        )
    # A release image reads /data/local/tmp flags for the selected debug app.
    # This setting does not require Chrome itself to be a debuggable APK.
    adb("shell", "am", "set-debug-app", "--persistent", CHROME_PACKAGE)
    adb(
        "shell",
        "echo 'chrome --disable-fre --no-first-run --no-default-browser-check "
        f"--remote-debugging-socket-name={CHROME_SOCKET}' "
        "> /data/local/tmp/chrome-command-line",
    )
    adb("shell", "chmod", "644", "/data/local/tmp/chrome-command-line")
    adb("shell", "am", "force-stop", CHROME_PACKAGE)
    details = adb("shell", "dumpsys", "package", CHROME_PACKAGE)
    version = next(
        (line.strip() for line in details.splitlines() if "versionName=" in line),
        "unknown",
    )
    logger.info("preinstalled mobile Chrome: %s", version)


def require_debuggable(package: str) -> None:
    """Check that geckodriver can manage app-private storage without root."""
    try:
        output = adb("shell", "run-as", package, "id")
    except AndroidError as exc:
        raise AndroidError(
            f"Firefox app storage requires a debuggable APK for {package}. "
            "Supply ANDROID_FIREFOX_APK_URL and ANDROID_FIREFOX_PACKAGE for a "
            f"debuggable build matching the emulator ABI. run-as failed: {exc}"
        ) from exc
    if "uid=" not in output:
        raise AndroidError(
            f"run-as did not return an app identity for {package}: {output.strip()}"
        )
    logger.info("Firefox app-storage preflight: %s", output.strip())


def forward_chrome_devtools(port: int = CHROME_DEVTOOLS_PORT) -> str:
    """Expose Chrome's on-device DevTools socket on a host port."""
    # Preserve geckodriver's forwards when both browsers share the device.
    adb("forward", "--remove", f"tcp:{port}", check=False)
    adb("forward", f"tcp:{port}", f"localabstract:{CHROME_SOCKET}")
    # A forward can succeed before the HTTP server is ready. Discover the
    # browser websocket from CDP rather than guessing its path.
    deadline = time.monotonic() + 60
    error = "missing webSocketDebuggerUrl"
    while time.monotonic() < deadline:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=5
            ) as response:
                version = json.load(response)
            endpoint = version.get("webSocketDebuggerUrl")
            if endpoint:
                logger.info(
                    "forwarded %s DevTools to %s", version.get("Browser"), endpoint
                )
                return endpoint
        except (OSError, ValueError) as exc:
            error = str(exc)
        time.sleep(1)
    raise AndroidError(f"Chrome DevTools on port {port} did not become ready: {error}")


class Emulator:
    """A running Android emulator, usable as a context manager."""

    def __init__(
        self,
        avd_name: str = AVD_NAME,
        *,
        log_path: Path | None = None,
    ) -> None:
        self.avd_name = avd_name
        self.log_path = log_path
        self.process: subprocess.Popen | None = None

    def start(self) -> Self:
        prepare_adb()
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
            "-memory",
            "4096",
            "-camera-back",
            "none",
            "-camera-front",
            "none",
        ]
        logger.info("starting emulator %s", self.avd_name)
        if self.log_path is not None:
            with self.log_path.open("w") as log:
                self.process = subprocess.Popen(
                    cmd, stdout=log, stderr=subprocess.STDOUT
                )
        else:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        try:
            self.wait_for_boot()
        except Exception:
            if self.log_path is not None and self.log_path.is_file():
                logger.error(
                    "Emulator log tail:\n%s",
                    self.log_path.read_text(errors="replace")[-4000:],
                )
            self.stop(force=True)
            raise
        return self

    def wait_for_boot(self, timeout: int = BOOT_TIMEOUT) -> None:
        """Block until the device reports a completed boot."""
        deadline = time.monotonic() + timeout
        unauthorized_since = None
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise AndroidError(
                    f"emulator exited with {self.process.returncode} before booting"
                )
            waited = int(time.monotonic() - (deadline - timeout))
            # get-state/getprop put authorization errors on stderr. Listing
            # devices exposes the real transport state instead of reporting
            # an empty stdout as if Android were still booting.
            devices = adb("devices", "-l", timeout=10)
            state = next(
                (
                    parts[1]
                    for line in devices.splitlines()
                    if len(parts := line.split()) >= 2 and parts[0] == DEVICE_SERIAL
                ),
                "not connected",
            )
            if state == "unauthorized":
                if unauthorized_since is None:
                    unauthorized_since = time.monotonic()
                if time.monotonic() - unauthorized_since >= ADB_AUTH_TIMEOUT:
                    raise AndroidError(
                        f"{DEVICE_SERIAL}: ADB device unauthorized for {ADB_AUTH_TIMEOUT}s; "
                        "the emulator and ADB server must use the same authentication key "
                        "(ANDROID_USER_HOME / ADB_VENDOR_KEYS)."
                    )
            else:
                unauthorized_since = None
            if state == "not connected" and waited >= ADB_CONNECT_TIMEOUT:
                raise AndroidError(
                    f"{DEVICE_SERIAL} did not connect to ADB within {ADB_CONNECT_TIMEOUT}s"
                )
            if waited and waited % 60 < BOOT_POLL_INTERVAL:
                logger.info(
                    "waiting for boot (%ss): state=%r",
                    waited,
                    state,
                )
            if state == "device" and getprop("sys.boot_completed") == "1":
                logger.info(
                    "emulator booted: Android %s, %s",
                    getprop("ro.build.version.release"),
                    getprop("ro.product.cpu.abi"),
                )
                return
            time.sleep(BOOT_POLL_INTERVAL)
        raise AndroidError(f"emulator did not boot within {timeout}s")

    def stop(self, *, force: bool = False) -> None:
        if self.process is None:
            return
        if force:
            if self.process.poll() is None:
                self.process.terminate()
        else:
            adb("emu", "kill", check=False, timeout=30)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *args, **kwargs) -> None:
        self.stop()
