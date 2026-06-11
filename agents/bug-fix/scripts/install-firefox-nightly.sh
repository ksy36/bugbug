#!/usr/bin/env bash
# Download and install the latest Firefox Nightly (linux64) to /opt/firefox.
# Used at image-build time for web-compat reproduction (WEBCOMPAT_TOOLS=true).
# Nightly is only as fresh as the last image build — rebuild to update it.
set -euo pipefail

DEST="${1:-/opt/firefox}"
URL="https://download.mozilla.org/?product=firefox-nightly-latest-ssl&os=linux64&lang=en-US"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading Firefox Nightly..."
curl -fSL --retry 3 -o "$tmp/firefox.tar.xz" "$URL"

echo "Extracting to $DEST..."
mkdir -p "$DEST"
# The tarball contains a top-level firefox/ dir; strip it into $DEST.
tar -xJf "$tmp/firefox.tar.xz" -C "$DEST" --strip-components=1

echo "Installed: $("$DEST/firefox" --version)"
