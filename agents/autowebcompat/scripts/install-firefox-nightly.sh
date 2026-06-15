#!/usr/bin/env bash
# Download and install the latest Firefox Nightly to /opt/firefox.
# Used at image-build time for web-compat reproduction.
#
# Picks the Firefox build matching the host arch so an arm64 image gets native
# aarch64 Firefox (fast) and an amd64 image gets x86-64 (no qemu either way):
#   x86_64           -> os=linux64        (firefox-*.linux-x86_64.tar.xz)
#   aarch64 / arm64  -> os=linux64-aarch64 (firefox-*.linux-aarch64.tar.xz)
set -euo pipefail

DEST="${1:-/opt/firefox}"

arch="$(uname -m)"
case "$arch" in
  x86_64 | amd64)        os="linux64" ;;
  aarch64 | arm64)       os="linux64-aarch64" ;;
  *) echo "Unsupported arch for Firefox Nightly: $arch" >&2; exit 1 ;;
esac
URL="https://download.mozilla.org/?product=firefox-nightly-latest-ssl&os=${os}&lang=en-US"
echo "Host arch: $arch -> Firefox os=$os"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading Firefox Nightly..."
curl -fSL --retry 3 -o "$tmp/firefox.tar.xz" "$URL"

echo "Extracting to $DEST..."
mkdir -p "$DEST"
# The tarball contains a top-level firefox/ dir; strip it into $DEST.
tar -xJf "$tmp/firefox.tar.xz" -C "$DEST" --strip-components=1

echo "Installed: $("$DEST/firefox" --version)"
