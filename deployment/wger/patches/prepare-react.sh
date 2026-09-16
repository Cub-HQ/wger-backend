#!/bin/bash
# Rebuild reviewed UI overrides from the exact upstream image, without starting it.
set -euo pipefail
PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$PATCH_DIR/.." && pwd)
LIMA=/usr/local/bin/limactl
VM=fitness-wger
VM_DIR=/home/OPERATOR.guest/fitness-wger
IMAGE=docker.io/wger/server:2.7@sha256:1c5789b93bfe5eed0b7287255782d9177027b255de2b22b59f511a693a48db04
EXTRACTOR="fitness-wger-source-$$"
EXTRACTOR_CREATED=0
cleanup() {
  if [ "$EXTRACTOR_CREATED" = 1 ]; then
    "$LIMA" shell "$VM" docker rm "$EXTRACTOR" >/dev/null
  fi
}
trap cleanup EXIT
mkdir -p "$DEPLOY_DIR/overrides"
"$LIMA" shell "$VM" mkdir -p "$VM_DIR/overrides"
"$LIMA" shell "$VM" docker create --name "$EXTRACTOR" --entrypoint /bin/true "$IMAGE" >/dev/null
EXTRACTOR_CREATED=1
"$LIMA" shell "$VM" docker cp "$EXTRACTOR:/home/wger/src/node_modules/@wger-project/react-components/build/main.js" "$VM_DIR/overrides/react-original-main.js"
"$LIMA" shell "$VM" docker cp "$EXTRACTOR:/home/wger/src/wger/core/templates/template.html" "$VM_DIR/overrides/template-original.html"
"$LIMA" copy "$VM:$VM_DIR/overrides/react-original-main.js" "$DEPLOY_DIR/overrides/react-original-main.js"
"$LIMA" copy "$VM:$VM_DIR/overrides/template-original.html" "$DEPLOY_DIR/overrides/template-original.html"
/usr/local/bin/python3 "$PATCH_DIR/patch_muscle_diagram.py" "$DEPLOY_DIR/overrides/react-original-main.js" "$DEPLOY_DIR/overrides/react-main.js"
/usr/local/bin/python3 "$PATCH_DIR/patch_calendar_details.py" "$DEPLOY_DIR/overrides/react-main.js" "$DEPLOY_DIR/overrides/react-main.js"
/usr/local/bin/python3 "$PATCH_DIR/patch_footer.py" "$DEPLOY_DIR/overrides/template-original.html" "$DEPLOY_DIR/overrides/template.html"
"$LIMA" copy "$DEPLOY_DIR/overrides/react-main.js" "$VM:$VM_DIR/overrides/react-main.js"
"$LIMA" copy "$DEPLOY_DIR/overrides/template.html" "$VM:$VM_DIR/overrides/template.html"
