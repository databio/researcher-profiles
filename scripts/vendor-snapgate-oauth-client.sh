#!/usr/bin/env bash
# Vendor snapgate's OAuth client byte-for-byte. Never edit the copy; re-run this.
#   scripts/vendor-snapgate-oauth-client.sh [--check] [path/to/snapgate]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK=0; if [[ "${1:-}" == "--check" ]]; then CHECK=1; shift; fi
UPSTREAM="$(cd "${1:-$REPO/../../webdev/snapgate}" && pwd)"
SRC="$UPSTREAM/backend/snapgate/oauth_client"
DEST="$REPO/rp-sdk/src/researcher_profiles/_vendor/snapgate_oauth_client"
[[ -f "$SRC/__init__.py" ]] || { echo "no oauth_client at $SRC" >&2; exit 1; }
if (( CHECK )); then
  diff -r -x '__pycache__' "$SRC" "$DEST" && echo "vendored copy matches upstream"; exit
fi
mkdir -p "$DEST"
rsync -a --delete --exclude='__pycache__' --include='*.py' --include='README.md' \
    --include='LICENSE' --exclude='*' "$SRC/" "$DEST/"
echo "vendored snapgate oauth_client $(grep -E '^__version__' "$DEST/__init__.py" | cut -d'"' -f2)"
