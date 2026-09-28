#!/bin/bash
# Release owner entrypoint. Does not run during build/test.
set -euo pipefail
PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
exec python3 "$PATCH_DIR/release_web.py"
