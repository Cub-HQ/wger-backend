#!/bin/bash
# Build reviewed overrides locally against the configured Docker endpoint.
# Outputs .next files only; release-web.sh owns the separate release window.
# --backend-image instead builds the pinned backend source image and stages its provenance only.
set -euo pipefail

PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$PATCH_DIR/.." && pwd)
DOCKER_HOST=${WGER_DOCKER_HOST-unix://$HOME/.colima/default/docker.sock}
REACT_REPO=https://github.com/Cubatica/react
REACT_COMMIT=3066f7693ac00632ad14ea0ef025371156f91d0d
WGER_REPO=https://github.com/Cubatica/wger
WGER_COMMIT=135d8569a3eb27c9f0f74e865d56372421a61294
EXTRACTOR="fitness-wger-source-$$"
BACKEND_REPO=https://github.com/Cub-HQ/wger-backend
BACKEND_COMMIT=0812ca39a80e82c071c99a54300df73e28668776
BACKEND_TAG=fitness-wger-backend:$BACKEND_COMMIT
BACKEND_MATERIAL='wger/formats/en_AU/formats.py wger/utils/pdf.py wger/core/templates/template.html
  wger/exercises/templates/history/overview.html wger/core/templates/user/api_key.html
  wger/manager/models/session_recovery.py wger/manager/migrations/0031_workoutsessionrecovery.py
  wger/manager/tasks.py wger/exercises/models/video.py wger/exercises/migrations/0042_historicalexercisevideo_source_url.py'

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

if [ "${1-}" = --backend-image ]; then
  failed() { echo "backend image failed: $*" >&2; exit 1; }
  # A failed preparation must never leave an older receipt for release_web.py to trust.
  rm -f "$DEPLOY_DIR/overrides/backend-image.json.next"
  mkdir -p "$HOME/.cache"
  BUILD_DIR=$(mktemp -d "$HOME/.cache/fitness-wger-backend.XXXXXX")
  trap 'rm -rf "$BUILD_DIR"' EXIT
  curl --fail --location --silent --show-error "$BACKEND_REPO/archive/$BACKEND_COMMIT.tar.gz" --output "$BUILD_DIR/source.tgz"
  archive_commit=$(gzip -dc "$BUILD_DIR/source.tgz" 2>/dev/null | git get-tar-commit-id || true)
  [ "$archive_commit" = "$BACKEND_COMMIT" ] || failed "archive commit '$archive_commit' is not $BACKEND_COMMIT"
  mkdir "$BUILD_DIR/src"
  tar -xzf "$BUILD_DIR/source.tgz" --strip-components=1 -C "$BUILD_DIR/src"
  # shellcheck disable=SC2086 # BACKEND_MATERIAL is a word list of fixed paths.
  python3 - "$BUILD_DIR/src" $BACKEND_MATERIAL >"$BUILD_DIR/manifest.json" <<'PY'
import hashlib, json, os, sys
root, material = sys.argv[1], sys.argv[2:]
manifest = {}
for top in ('wger', 'settings'):
    for directory, _, names in os.walk(os.path.join(root, top)):
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path) and not os.path.islink(path):
                manifest[os.path.relpath(path, root)] = hashlib.sha256(open(path, 'rb').read()).hexdigest()
missing = [path for path in material if path not in manifest]
if missing:
    sys.exit('source lacks accepted backend files: ' + ', '.join(missing))
json.dump(manifest, sys.stdout, sort_keys=True)
PY
  # Runs inside the image with no network or mounts: every archived wger/settings file must be
  # byte-identical in the application tree Django imports, with no private deploy files baked in.
  VERIFY_IMAGE=$(cat <<'PY'
import hashlib, json, os, sys
root, commit = sys.argv[1], sys.argv[2]
manifest = json.load(sys.stdin)
def digest(path):
    with open(path, 'rb') as source:
        return hashlib.sha256(source.read()).hexdigest()
bad = sorted(path for path, expected in manifest.items()
             if not os.path.isfile(os.path.join(root, path)) or digest(os.path.join(root, path)) != expected)
if bad:
    sys.exit(f'{len(bad)} files differ from source: ' + ', '.join(bad[:20]))
if os.environ.get('APP_BUILD_COMMIT') != commit:
    sys.exit(f'APP_BUILD_COMMIT is {os.environ.get("APP_BUILD_COMMIT")!r}, expected {commit}')
import wger, wger.formats.en_AU.formats  # models need a database; their bytes are compared above
if not os.path.realpath(wger.__file__).startswith(os.path.realpath(root) + os.sep):
    sys.exit('wger imports from ' + wger.__file__)
private = [os.path.join(directory, name) for directory, _, names in os.walk(root)
           for name in names if name == 'private.env' or name.endswith('.dump')]
if private:
    sys.exit('private files in image: ' + ', '.join(private))
print(f'image matches {len(manifest)} source files at {commit}')
PY
)
  verify_image() {
    docker -H "$DOCKER_HOST" run --rm -i --network none --entrypoint python3 "$1" \
      -c "$VERIFY_IMAGE" /home/wger/src "$BACKEND_COMMIT" <"$BUILD_DIR/manifest.json"
  }
  label() {
    docker -H "$DOCKER_HOST" image inspect --format "{{index .Config.Labels \"$1\"}}" "$2"
  }

  if docker -H "$DOCKER_HOST" image inspect "$BACKEND_TAG" >/dev/null 2>&1; then
    IMAGE_ID=$(docker -H "$DOCKER_HOST" image inspect --format '{{.Id}}' "$BACKEND_TAG")
    revision=$(label org.opencontainers.image.revision "$IMAGE_ID")
    BASE_IMAGE=$(label org.opencontainers.image.base.name "$IMAGE_ID")
    [ "$revision" = "$BACKEND_COMMIT" ] && [ -n "$BASE_IMAGE" ] ||
      failed "existing $BACKEND_TAG records revision '$revision' base '$BASE_IMAGE'; tag not overwritten"
    verify_image "$IMAGE_ID" || failed "existing $BACKEND_TAG failed source verification; tag not overwritten"
  else
    docker -H "$DOCKER_HOST" pull --quiet wger/base:latest >/dev/null
    BASE_IMAGE=$(docker -H "$DOCKER_HOST" image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' wger/base:latest |
      grep -m1 'wger/base@sha256:' || true)
    [ -n "$BASE_IMAGE" ] || failed 'wger/base:latest has no registry digest'
    # Pin FROM wger/base:latest to the digest just resolved, so the recorded base is the one built on.
    docker -H "$DOCKER_HOST" build --file "$BUILD_DIR/src/extras/docker/production/Dockerfile" \
      --build-context "wger/base:latest=docker-image://$BASE_IMAGE" \
      --build-arg "BUILD_COMMIT=$BACKEND_COMMIT" --build-arg "BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --label "org.opencontainers.image.source=$BACKEND_REPO" \
      --label "org.opencontainers.image.revision=$BACKEND_COMMIT" \
      --label "org.opencontainers.image.base.name=$BASE_IMAGE" \
      --iidfile "$BUILD_DIR/image-id" "$BUILD_DIR/src"
    IMAGE_ID=$(cat "$BUILD_DIR/image-id")
    verify_image "$IMAGE_ID" || failed "built image $IMAGE_ID failed source verification; not tagged"
    docker -H "$DOCKER_HOST" tag "$IMAGE_ID" "$BACKEND_TAG"
  fi
  APP_BUILD_COMMIT=$(docker -H "$DOCKER_HOST" image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMAGE_ID" |
    sed -n 's/^APP_BUILD_COMMIT=//p')
  mkdir -p "$DEPLOY_DIR/overrides"
  python3 - "$DEPLOY_DIR/overrides/backend-image.json.next" "$BUILD_DIR/source.tgz" "$BACKEND_REPO" \
    "$BACKEND_COMMIT" "$BACKEND_TAG" "$IMAGE_ID" "$APP_BUILD_COMMIT" "$BASE_IMAGE" <<'PY'
import hashlib, json, os, sys
target, archive, repository, commit, tag, image_id, app_build_commit, base_image = sys.argv[1:]
with open(archive, 'rb') as source:
    archive_sha256 = hashlib.sha256(source.read()).hexdigest()
record = {'repository': repository, 'commit': commit, 'archive_sha256': archive_sha256,
          'dockerfile': 'extras/docker/production/Dockerfile', 'base_image': base_image,
          'image_id': image_id, 'tag': tag, 'app_build_commit': app_build_commit}
with open(target + '.tmp', 'w') as output:
    json.dump(record, output, indent=2, sort_keys=True)
    output.write('\n')
os.replace(target + '.tmp', target)
PY
  echo "backend image $BACKEND_TAG ($IMAGE_ID) staged in overrides/backend-image.json.next; no live service was changed"
  exit 0
fi
IMAGE=$(docker -H "$DOCKER_HOST" compose -f "$DEPLOY_DIR/compose.yaml" config --no-env-resolution --format json | python3 -c '
import json, sys
image = json.load(sys.stdin)["services"]["web"]["image"]
if not isinstance(image, str) or not image.strip():
    sys.exit(1)
print(image)
') || preflight_failed 'compose web image could not be resolved'
docker -H "$DOCKER_HOST" image inspect "$IMAGE" >/dev/null 2>&1 || preflight_failed "image unavailable: $IMAGE"

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

curl --fail --location --silent --show-error "$REACT_REPO/archive/$REACT_COMMIT.tar.gz" --output "$BUILD_DIR/react-source.tgz"
tar -xzf "$BUILD_DIR/react-source.tgz" --strip-components=1 -C "$BUILD_DIR"
(cd "$BUILD_DIR" && npm ci --ignore-scripts --no-audit --no-fund && python3 "$PATCH_DIR/patch_australian_dates.py" "$BUILD_DIR" && python3 "$PATCH_DIR/patch_progression_chart.py" "$BUILD_DIR" && python3 "$PATCH_DIR/patch_session_recovery_ui.py" "$BUILD_DIR" && python3 "$PATCH_DIR/test_patch_session_recovery_ui.py" --install "$BUILD_DIR" && npm test -- src/core/lib/date.test.ts src/components/Routines/screens/Detail/SessionRecovery.test.tsx && npm run typecheck && npm run build)
python3 "$PATCH_DIR/patch_muscle_diagram.py" "$BUILD_DIR/build/main.js" "$STAGED_DIR/react-main.js.next"

cat > "$STAGED_DIR/corresponding-source.json.next" <<EOF
{"license":"AGPL-3.0","server":{"repository":"$WGER_REPO","commit":"$WGER_COMMIT"},"frontend":{"repository":"$REACT_REPO","commit":"$REACT_COMMIT","upstream_commit":"89d234a800ba0f2097162f1d91444c7e3a5ccc5c"}}
EOF

# Publish candidates only once every strict pinned-source transformation succeeds.
for name in react-main.js corresponding-source.json; do
  test -f "$STAGED_DIR/$name.next"
done
for artifact in "$STAGED_DIR/"*.next; do
  cp "$artifact" "$DEPLOY_DIR/overrides/"
done

echo 'reviewed overrides staged as .next files; no live service was changed'
