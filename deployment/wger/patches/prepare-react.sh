#!/bin/bash
# Build reviewed overrides locally against the configured Docker endpoint.
# Outputs .next files only; release-web.sh owns the separate release window.
set -euo pipefail

PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$PATCH_DIR/.." && pwd)
DOCKER_HOST=${WGER_DOCKER_HOST-unix://$HOME/.colima/default/docker.sock}
REACT_REPO=https://github.com/Cubatica/react
REACT_COMMIT=3066f7693ac00632ad14ea0ef025371156f91d0d
WGER_REPO=https://github.com/Cubatica/wger
WGER_COMMIT=135d8569a3eb27c9f0f74e865d56372421a61294
EXTRACTOR="fitness-wger-source-$$"

preflight_failed() {
  echo "preflight failed: $*" >&2
  exit 1
}

case "$DOCKER_HOST" in
  unix:///*) ;;
  *) preflight_failed "Docker endpoint unsupported: $DOCKER_HOST (expected unix:///path)" ;;
esac
[ -S "${DOCKER_HOST#unix://}" ] || preflight_failed "Docker socket unavailable: ${DOCKER_HOST#unix://}"
docker -H "$DOCKER_HOST" info >/dev/null 2>&1 || preflight_failed "Docker daemon unreachable: $DOCKER_HOST"
IMAGE=$(docker -H "$DOCKER_HOST" compose -f "$DEPLOY_DIR/compose.yaml" config --no-env-resolution --format json | python3 -c '
import json, sys
image = json.load(sys.stdin)["services"]["web"]["image"]
if not isinstance(image, str) or not image.strip():
    sys.exit(1)
print(image)
') || preflight_failed 'compose web image could not be resolved'
docker -H "$DOCKER_HOST" image inspect "$IMAGE" >/dev/null 2>&1 || preflight_failed "image unavailable: $IMAGE"

# Exercise the same bind, image user and Python environment as the ORM proof.
probe_status=0
docker -H "$DOCKER_HOST" run --rm --network none --user wger --entrypoint /bin/sh \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --mount "type=bind,src=$PATCH_DIR,target=/tests,readonly" \
  "$IMAGE" -c '
    cat /tests/test_patch_session_recovery.py /tests/patch_session_recovery.py >/dev/null || exit 41
    python3 -c "pass" || exit 42
    python3 -c '\''
import importlib, sys
for module in ("django", "rest_framework.exceptions", "sqlite3"):
    try:
        importlib.import_module(module)
    except Exception as error:
        print(f"preflight failed: module importability: {module}: {error}", file=sys.stderr)
        sys.exit(1)
'\'' || exit 43
  ' || probe_status=$?
case "$probe_status" in
  0) ;;
  41) preflight_failed "staging path unreadable in image: $PATCH_DIR" ;;
  42) preflight_failed "Python interpreter unavailable for wger in image: $IMAGE" ;;
  43) exit 1 ;;
  *) preflight_failed "staging path/container probe could not run: $PATCH_DIR (image $IMAGE, status $probe_status)" ;;
esac

BUILD_DIR=$(mktemp -d)
STAGED_DIR="$BUILD_DIR/staged"
mkdir -p "$STAGED_DIR"
cleanup() {
  docker -H "$DOCKER_HOST" rm "$EXTRACTOR" >/dev/null 2>&1 || true
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT
mkdir -p "$DEPLOY_DIR/overrides"

docker -H "$DOCKER_HOST" create --name "$EXTRACTOR" --entrypoint /bin/true "$IMAGE" >/dev/null
docker -H "$DOCKER_HOST" cp "$EXTRACTOR:/home/wger/src/node_modules/@wger-project/react-components/build/main.js" "$DEPLOY_DIR/overrides/react-original-main.js"
docker -H "$DOCKER_HOST" cp "$EXTRACTOR:/home/wger/src/wger/core/templates/template.html" "$DEPLOY_DIR/overrides/template-original.html"

curl --fail --location --silent --show-error "$REACT_REPO/archive/$REACT_COMMIT.tar.gz" --output "$BUILD_DIR/react-source.tgz"
tar -xzf "$BUILD_DIR/react-source.tgz" --strip-components=1 -C "$BUILD_DIR"
(cd "$BUILD_DIR" && npm ci --ignore-scripts --no-audit --no-fund && python3 "$PATCH_DIR/patch_australian_dates.py" "$BUILD_DIR" && python3 "$PATCH_DIR/patch_progression_chart.py" "$BUILD_DIR" && python3 "$PATCH_DIR/patch_session_recovery_ui.py" "$BUILD_DIR" && python3 "$PATCH_DIR/test_patch_session_recovery_ui.py" --install "$BUILD_DIR" && npm test -- src/core/lib/date.test.ts src/components/Routines/screens/Detail/SessionRecovery.test.tsx && npm run typecheck && npm run build)
python3 "$PATCH_DIR/patch_muscle_diagram.py" "$BUILD_DIR/build/main.js" "$STAGED_DIR/react-main.js.next"

curl --fail --location --silent --show-error "$WGER_REPO/raw/$WGER_COMMIT/wger/core/templates/template.html" --output "$BUILD_DIR/template.html"
python3 "$PATCH_DIR/patch_footer.py" "$BUILD_DIR/template.html" "$STAGED_DIR/template.html.next"

curl --fail --location --silent --show-error "$WGER_REPO/raw/$WGER_COMMIT/wger/exercises/templates/history/overview.html" --output "$BUILD_DIR/history-overview.html"
python3 "$PATCH_DIR/patch_australian_template_dates.py" history-overview "$BUILD_DIR/history-overview.html" "$STAGED_DIR/history-overview.html.next"

curl --fail --location --silent --show-error "$WGER_REPO/raw/$WGER_COMMIT/wger/core/templates/user/api_key.html" --output "$BUILD_DIR/api-key.html"
python3 "$PATCH_DIR/patch_australian_template_dates.py" api-key "$BUILD_DIR/api-key.html" "$STAGED_DIR/api-key.html.next"
curl --fail --location --silent --show-error "$WGER_REPO/raw/$WGER_COMMIT/wger/utils/pdf.py" --output "$BUILD_DIR/pdf.py"
python3 "$PATCH_DIR/patch_australian_pdf.py" "$BUILD_DIR/pdf.py" "$STAGED_DIR/pdf.py.next"

mkdir "$BUILD_DIR/server"
curl --fail --location --silent --show-error "$WGER_REPO/archive/$WGER_COMMIT.tar.gz" --output "$BUILD_DIR/server-source.tgz"
tar -xzf "$BUILD_DIR/server-source.tgz" --strip-components=1 -C "$BUILD_DIR/server"
python3 "$PATCH_DIR/patch_session_recovery.py" "$BUILD_DIR/server" "$STAGED_DIR"
# Use the released image's dependencies, never the host environment or live database.
docker -H "$DOCKER_HOST" run --rm --network none --user wger --entrypoint python3 \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --mount "type=bind,src=$PATCH_DIR,target=/tests,readonly" \
  "$IMAGE" /tests/test_patch_session_recovery.py --orm


cat > "$STAGED_DIR/corresponding-source.json.next" <<EOF
{"license":"AGPL-3.0","server":{"repository":"$WGER_REPO","commit":"$WGER_COMMIT"},"frontend":{"repository":"$REACT_REPO","commit":"$REACT_COMMIT","upstream_commit":"89d234a800ba0f2097162f1d91444c7e3a5ccc5c"}}
EOF

# Publish candidates only once every strict pinned-source transformation succeeds.
for name in react-main.js template.html history-overview.html api-key.html pdf.py corresponding-source.json \
  manager-session-recovery.py manager-models-init.py manager-api-views.py manager-tasks.py \
  manager-log.py manager-0030-workoutlog-cardio-metrics.py manager-0031-session-recovery.py; do
  test -f "$STAGED_DIR/$name.next"
done
for artifact in "$STAGED_DIR/"*.next; do
  cp "$artifact" "$DEPLOY_DIR/overrides/"
done

echo 'reviewed overrides staged as .next files; no live service was changed'
