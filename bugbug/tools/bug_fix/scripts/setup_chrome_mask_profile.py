#!/usr/bin/env python3
"""Build a Firefox profile with the Chrome Mask extension installed.

Chrome Mask spoofs the User-Agent to Chrome. Pre-installing it into a profile
(rather than having the agent install it at runtime) avoids needing the
privileged-context / MOZ_REMOTE_ALLOW_SYSTEM_ACCESS path in the devtools MCP:
the MCP just uses this profile via --profile-path, and the agent enables the
mask per-site through the extension's own options page.

The extension is *registered* by this script, not enabled for any site — the
agent does the per-site enabling at runtime (it can't be scripted headlessly).

Typical use:

    python -m bugbug.tools.bug_fix.scripts.setup_chrome_mask_profile \\
        --profile-dir ~/.cache/bug-fix/chrome-mask-profile \\
        --firefox /usr/bin/firefox

Then point a run at it:

    python -m bugbug.tools.bug_fix --webcompat-tools \\
        --firefox-path /usr/bin/firefox \\
        --chrome-mask-profile ~/.cache/bug-fix/chrome-mask-profile ...
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

# AMO per-addon endpoint; resolves the current signed xpi URL at run time so we
# never pin a stale version.
_AMO_API = "https://addons.mozilla.org/api/v5/addons/addon/chrome-mask/"


def resolve_xpi_url() -> tuple[str, str]:
    """Return (download_url, version) for the latest signed Chrome Mask xpi."""
    req = urllib.request.Request(_AMO_API, headers={"User-Agent": "bug-fix-setup"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    ver = data["current_version"]
    return ver["file"]["url"], ver["version"]


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "bug-fix-setup"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def extract_extension_id(xpi: Path) -> str:
    """Read the gecko extension ID out of the xpi's manifest.json."""
    with zipfile.ZipFile(xpi) as zf, zf.open("manifest.json") as f:
        manifest = json.load(f)
    for key in ("browser_specific_settings", "applications"):
        gecko = manifest.get(key, {}).get("gecko", {})
        if "id" in gecko:
            return gecko["id"]
    raise SystemExit(f"no gecko extension ID in {xpi}'s manifest.json")


def create_profile(firefox: str, name: str, path: Path) -> None:
    result = subprocess.run(
        [firefox, "-CreateProfile", f"{name} {path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"firefox -CreateProfile failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


def install_xpi(profile_dir: Path, xpi: Path, ext_id: str) -> Path:
    ext_dir = profile_dir / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    dest = ext_dir / f"{ext_id}.xpi"
    shutil.copy2(xpi, dest)
    return dest


def warm_launch(firefox: str, profile_name: str, timeout: int = 15) -> None:
    """Run Firefox headless briefly so it scans + registers the dropped xpi."""
    proc = subprocess.Popen(
        [firefox, "-P", profile_name, "-headless", "-no-remote", "about:blank"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def verify_registered(profile_dir: Path, ext_id: str) -> bool:
    ext_json = profile_dir / "extensions.json"
    if not ext_json.exists():
        return False
    try:
        data = json.loads(ext_json.read_text())
    except json.JSONDecodeError:
        return False
    return any(a.get("id") == ext_id for a in data.get("addons", []))


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--profile-dir",
        type=Path,
        required=True,
        help="Where to create the profile (reused via --chrome-mask-profile).",
    )
    p.add_argument(
        "--profile-name",
        default="bug-fix-chrome-mask",
        help="Name for 'firefox -P <name>' invocations.",
    )
    p.add_argument(
        "--firefox",
        default="firefox",
        help="Firefox binary to use (default: firefox on $PATH).",
    )
    p.add_argument(
        "--xpi",
        type=Path,
        default=None,
        help="Use a local chrome-mask xpi instead of downloading from AMO.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Wipe and recreate profile-dir if it exists.",
    )
    args = p.parse_args()

    if args.profile_dir.exists():
        if not args.force:
            raise SystemExit(
                f"profile-dir {args.profile_dir} exists; pass --force to recreate."
            )
        shutil.rmtree(args.profile_dir)

    if args.xpi:
        xpi = args.xpi
        if not xpi.is_file():
            raise SystemExit(f"xpi not found: {xpi}")
        cleanup_xpi = False
    else:
        url, version = resolve_xpi_url()
        print(f"[setup] downloading Chrome Mask {version} from AMO", file=sys.stderr)
        xpi = args.profile_dir.parent / "chrome-mask.xpi"
        xpi.parent.mkdir(parents=True, exist_ok=True)
        download(url, xpi)
        cleanup_xpi = True

    ext_id = extract_extension_id(xpi)
    print(f"[setup] extension ID: {ext_id}", file=sys.stderr)

    print(f"[setup] creating profile at {args.profile_dir}", file=sys.stderr)
    create_profile(args.firefox, args.profile_name, args.profile_dir)
    install_xpi(args.profile_dir, xpi, ext_id)

    print("[setup] warm-launching Firefox to register the extension", file=sys.stderr)
    warm_launch(args.firefox, args.profile_name)
    time.sleep(1)

    if cleanup_xpi:
        xpi.unlink(missing_ok=True)

    if verify_registered(args.profile_dir, ext_id):
        print(f"[setup] success — Chrome Mask registered in {args.profile_dir}")
        print("[setup] pass it to a run with:")
        print(f"          --chrome-mask-profile {args.profile_dir}")
        return 0

    print(
        "[setup] WARNING: xpi copied but not registered in extensions.json; "
        f"try launching once: {args.firefox} -P {args.profile_name}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
