#!/bin/bash
# Build reviewed overrides locally against the current Colima Docker context.
# Outputs .next files only; release-web.sh owns the separate release window.
set -euo pipefail

PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$PATCH_DIR/.." && pwd)
DOCKER_HOST=${WGER_DOCKER_HOST:-unix://$HOME/.colima/default/docker.sock}
IMAGE=ghcr.io/cubatica/fitness-wger:135d8569a3eb27c9f0f74e865d56372421a61294
REACT_REPO=https://github.com/Cubatica/react
REACT_COMMIT=3066f7693ac00632ad14ea0ef025371156f91d0d
WGER_REPO=https://github.com/Cubatica/wger
WGER_COMMIT=135d8569a3eb27c9f0f74e865d56372421a61294
EXTRACTOR="fitness-wger-source-$$"
BUILD_DIR=$(mktemp -d)
cleanup() {
  docker -H "$DOCKER_HOST" rm "$EXTRACTOR" >/dev/null 2>&1 || true
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

[ "$DOCKER_HOST" = "unix://$HOME/.colima/default/docker.sock" ] || { echo 'wrong Docker host' >&2; exit 1; }
[ -S "${DOCKER_HOST#unix://}" ] || { echo 'reviewed Colima socket is unavailable' >&2; exit 1; }
mkdir -p "$DEPLOY_DIR/overrides"

docker -H "$DOCKER_HOST" create --name "$EXTRACTOR" --entrypoint /bin/true "$IMAGE" >/dev/null
docker -H "$DOCKER_HOST" cp "$EXTRACTOR:/home/wger/src/node_modules/@wger-project/react-components/build/main.js" "$DEPLOY_DIR/overrides/react-original-main.js"
docker -H "$DOCKER_HOST" cp "$EXTRACTOR:/home/wger/src/wger/core/templates/template.html" "$DEPLOY_DIR/overrides/template-original.html"

curl --fail --location --silent --show-error "$REACT_REPO/archive/$REACT_COMMIT.tar.gz" --output "$BUILD_DIR/react-source.tgz"
tar -xzf "$BUILD_DIR/react-source.tgz" --strip-components=1 -C "$BUILD_DIR"
(cd "$BUILD_DIR" && npm ci --ignore-scripts --no-audit --no-fund && python3 "$PATCH_DIR/patch_australian_dates.py" "$BUILD_DIR" && python3 "$PATCH_DIR/patch_progression_chart.py" "$BUILD_DIR" && npm run typecheck && npm run build)
python3 "$PATCH_DIR/patch_muscle_diagram.py" "$BUILD_DIR/build/main.js" "$DEPLOY_DIR/overrides/react-main.js.next"

curl --fail --location --silent --show-error "$WGER_REPO/raw/$WGER_COMMIT/wger/core/templates/template.html" --output "$BUILD_DIR/template.html"
python3 "$PATCH_DIR/patch_footer.py" "$BUILD_DIR/template.html" "$DEPLOY_DIR/overrides/template.html.next"

cat > "$DEPLOY_DIR/overrides/corresponding-source.json.next" <<EOF
{"license":"AGPL-3.0","server":{"repository":"$WGER_REPO","commit":"$WGER_COMMIT"},"frontend":{"repository":"$REACT_REPO","commit":"$REACT_COMMIT","upstream_commit":"89d234a800ba0f2097162f1d91444c7e3a5ccc5c"}}
EOF

echo 'reviewed overrides staged as .next files; no live service was changed'
