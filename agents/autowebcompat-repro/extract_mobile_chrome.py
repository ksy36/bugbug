# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Export preinstalled mobile Chrome from the chrome-donor Docker target.

Run with KVM. No Play account, app-store download, or root access is required.
The donor and test emulator must use the same Android API and architecture.
"""

import hashlib
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

from hackbot_agents.autowebcompat_repro import android


def export_chrome(destination: Path) -> None:
    """Copy Chrome's APKs and preinstalled Trichrome static libraries."""
    chrome_paths = [
        line.removeprefix("package:").strip()
        for line in android.adb(
            "shell", "pm", "path", "com.android.chrome"
        ).splitlines()
        if line.startswith("package:")
    ]
    if not chrome_paths:
        raise android.AndroidError("The donor image has no com.android.chrome APKs")

    # Static shared libraries are not ordinary launchable packages and may
    # be hidden by pm list packages. Read their system APK paths directly.
    # Match the directory too: some images name the actual APK base.apk.
    listing = android.adb(
        "shell",
        "find",
        "-L",
        "/product",
        "/system",
        "/system_ext",
        "-type",
        "f",
        "-name",
        "*.apk",
        check=False,
    )
    libraries = defaultdict(list)
    for remote in sorted(set(listing.splitlines())):
        if "trichromelibrary" in remote.lower() and remote.endswith(".apk"):
            libraries[str(PurePosixPath(remote).parent)].append(remote)

    destination.mkdir(parents=True, exist_ok=True)
    packages = []
    # Aliases such as /system/product and /product can expose the same APK.
    seen_library_hashes = set()
    for index, (label, paths) in enumerate(
        [*libraries.items(), ("com.android.chrome", chrome_paths)]
    ):
        package = {"package": label, "apks": []}
        for apk_index, remote in enumerate(paths):
            relative = f"package-{index}/{apk_index}-{PurePosixPath(remote).name}"
            local = destination / relative
            local.parent.mkdir(parents=True, exist_ok=True)
            android.adb("pull", remote, str(local))
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            package["apks"].append({"file": relative, "sha256": digest})
        hashes = tuple(sorted(apk["sha256"] for apk in package["apks"]))
        if label != "com.android.chrome":
            if hashes in seen_library_hashes:
                continue
            seen_library_hashes.add(hashes)
        packages.append(package)

    manifest = {
        "api": android.getprop("ro.build.version.sdk"),
        "abi": android.getprop("ro.product.cpu.abi"),
        "fingerprint": android.getprop("ro.build.fingerprint"),
        "packages": packages,
    }
    details = android.adb("shell", "dumpsys", "package", "com.android.chrome")
    (destination / "chrome-package.txt").write_text(details)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2), flush=True)


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/mobile-chrome")
    artifacts = Path("/tmp/chrome-donor-artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        with android.Emulator(root=False, log_path=artifacts / "emulator.log"):
            try:
                export_chrome(destination)
            finally:
                for name, args in {
                    "logcat": ("logcat", "-d", "-t", "2000"),
                    "packages": ("shell", "dumpsys", "package", "com.android.chrome"),
                }.items():
                    try:
                        (artifacts / f"{name}.txt").write_text(
                            android.adb(*args, check=False, timeout=30)
                        )
                    except Exception:
                        logging.exception("Could not collect donor %s", name)
    except Exception:
        logging.exception("Mobile Chrome export failed")
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    main()
