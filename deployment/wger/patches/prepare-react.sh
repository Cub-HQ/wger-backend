#!/bin/bash
# Build reviewed overrides locally against the configured Docker endpoint.
# Outputs .next files only; release-web.sh owns the separate release window.
# --backend-image instead builds the pinned backend source image and stages its provenance only.
set -euo pipefail

PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$PATCH_DIR/.." && pwd)
DOCKER_HOST=${WGER_DOCKER_HOST-unix://$HOME/.colima/default/docker.sock}
WGER_REPO=https://github.com/Cubatica/wger
WGER_COMMIT=135d8569a3eb27c9f0f74e865d56372421a61294
EXTRACTOR="fitness-wger-source-$$"
BACKEND_REPO=https://github.com/Cub-HQ/wger-backend
BACKEND_COMMIT=920a516968d7dd2ffea06064d4c5dadef4196782
BACKEND_UPSTREAM='https://github.com/wger-project/wger 83005f7d487c814833f3943784370bb0149fbaa8'
BACKEND_TAG=fitness-wger-backend:$BACKEND_COMMIT
# The backend commit carries the reviewed frontend package; its source, archive and main.js are pinned here.
FRONTEND_REPO=https://github.com/Cub-HQ/wger-frontend
FRONTEND_COMMIT=b23cd36458ba921fe91b448bb3aded4a2bf99189
FRONTEND_UPSTREAM='https://github.com/wger-project/react 89d234a800ba0f2097162f1d91444c7e3a5ccc5c'
FRONTEND_PACKAGE=extras/docker/production/react-components/wger-project-react-components-26.8.28.tgz
FRONTEND_SHA256=c8b3e6b6d251067758f14eb176f961dd881216ef1985e3506e08a2e13a0e4ec0
FRONTEND_MAIN_JS_SHA256=b4396316a69dddfea75a16e78907c515ee5e27478a8c7e64dfc299f2a19074b4
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
  frontend_sha256=$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$BUILD_DIR/src/$FRONTEND_PACKAGE" 2>/dev/null || true)
  [ "$frontend_sha256" = "$FRONTEND_SHA256" ] ||
    failed "frontend package $FRONTEND_PACKAGE is '$frontend_sha256', not the reviewed $FRONTEND_SHA256"
  # shellcheck disable=SC2086 # BACKEND_MATERIAL is a word list of fixed paths.
  python3 - "$BUILD_DIR/src" "$FRONTEND_PACKAGE" "$FRONTEND_MAIN_JS_SHA256" $BACKEND_MATERIAL >"$BUILD_DIR/manifest.json" <<'PY'
import hashlib, json, os, sys, tarfile
root, package, main_js_sha256, material = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
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
# The Dockerfile unpacks the package with --strip-components=1; every member must land byte-identical.
installed = 'node_modules/@wger-project/react-components/'
with tarfile.open(os.path.join(root, package)) as archive:
    for member in archive.getmembers():
        if not member.isfile():
            continue
        relative = member.name.split('/', 1)[1]
        manifest[installed + relative] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
if manifest.get(installed + 'build/main.js') != main_js_sha256:
    sys.exit(f'frontend package build/main.js is not the reviewed {main_js_sha256}')
json.dump(manifest, sys.stdout, sort_keys=True)
PY
  # Runs inside the image with no network or mounts: every archived wger/settings file and every
  # frontend package file must be byte-identical in the tree Django imports and serves, the
  # installed package must hold nothing else, and no private deploy files may be baked in.
  VERIFY_IMAGE=$(cat <<'PY'
import hashlib, json, os, sys
root, commit, ui_commit = sys.argv[1], sys.argv[2], sys.argv[3]
manifest = json.load(sys.stdin)
def digest(path):
    with open(path, 'rb') as source:
        return hashlib.sha256(source.read()).hexdigest()
bad = sorted(path for path, expected in manifest.items()
             if not os.path.isfile(os.path.join(root, path)) or digest(os.path.join(root, path)) != expected)
if bad:
    sys.exit(f'{len(bad)} files differ from source: ' + ', '.join(bad[:20]))
installed = 'node_modules/@wger-project/react-components'
extra = sorted(os.path.relpath(os.path.join(directory, name), root)
               for directory, _, names in os.walk(os.path.join(root, installed)) for name in names)
extra = [path for path in extra if path not in manifest]
if extra:
    sys.exit(f'{len(extra)} files in the installed frontend package are not in the reviewed package: ' + ', '.join(extra[:20]))
if os.environ.get('APP_BUILD_COMMIT') != commit:
    sys.exit(f'APP_BUILD_COMMIT is {os.environ.get("APP_BUILD_COMMIT")!r}, expected {commit}')
if os.environ.get('APP_UI_BUILD_COMMIT') != ui_commit:
    sys.exit(f'APP_UI_BUILD_COMMIT is {os.environ.get("APP_UI_BUILD_COMMIT")!r}, expected {ui_commit}')
import wger, wger.formats.en_AU.formats  # models need a database; their bytes are compared above
if not os.path.realpath(wger.__file__).startswith(os.path.realpath(root) + os.sep):
    sys.exit('wger imports from ' + wger.__file__)
private = [os.path.join(directory, name) for directory, _, names in os.walk(root)
           for name in names if name == 'private.env' or name.endswith('.dump')]
if private:
    sys.exit('private files in image: ' + ', '.join(private))
print(f'image matches {len(manifest)} source and frontend package files at {commit}')
PY
)
  verify_image() {
    docker -H "$DOCKER_HOST" run --rm -i --network none --entrypoint python3 "$1" \
      -c "$VERIFY_IMAGE" /home/wger/src "$BACKEND_COMMIT" "$FRONTEND_COMMIT" <"$BUILD_DIR/manifest.json"
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
    "$BACKEND_COMMIT" "$BACKEND_TAG" "$IMAGE_ID" "$APP_BUILD_COMMIT" "$BASE_IMAGE" \
    "$FRONTEND_REPO" "$FRONTEND_COMMIT" "$FRONTEND_UPSTREAM" "$FRONTEND_PACKAGE" "$FRONTEND_SHA256" "$FRONTEND_MAIN_JS_SHA256" <<'PY'
import hashlib, json, os, sys
(target, archive, repository, commit, tag, image_id, app_build_commit, base_image,
 frontend_repository, frontend_commit, frontend_upstream, package, package_sha256, main_js_sha256) = sys.argv[1:]
with open(archive, 'rb') as source:
    archive_sha256 = hashlib.sha256(source.read()).hexdigest()
record = {'repository': repository, 'commit': commit, 'archive_sha256': archive_sha256,
          'dockerfile': 'extras/docker/production/Dockerfile', 'base_image': base_image,
          'image_id': image_id, 'tag': tag, 'app_build_commit': app_build_commit,
          'frontend': {'repository': frontend_repository, 'commit': frontend_commit,
                       'upstream': frontend_upstream, 'package': package,
                       'package_sha256': package_sha256, 'main_js_sha256': main_js_sha256}}
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
docker -H "$DOCKER_HOST" cp "$EXTRACTOR:/home/wger/src/wger/core/templates/template.html" "$DEPLOY_DIR/overrides/template-original.html"

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


# Corresponding source for what the image actually serves: the backend commit it was built from
# and the frontend commit its committed react-components package was built from.
cat > "$STAGED_DIR/corresponding-source.json.next" <<EOF
{"license":"AGPL-3.0","server":{"repository":"$BACKEND_REPO","commit":"$BACKEND_COMMIT","upstream":"${BACKEND_UPSTREAM% *}","upstream_commit":"${BACKEND_UPSTREAM#* }"},"frontend":{"repository":"$FRONTEND_REPO","commit":"$FRONTEND_COMMIT","upstream":"${FRONTEND_UPSTREAM% *}","upstream_commit":"${FRONTEND_UPSTREAM#* }"}}
EOF

# Publish candidates only once every strict pinned-source transformation succeeds.
for name in template.html history-overview.html api-key.html pdf.py corresponding-source.json \
  manager-session-recovery.py manager-models-init.py manager-api-views.py manager-tasks.py \
  manager-log.py manager-0030-workoutlog-cardio-metrics.py manager-0031-session-recovery.py; do
  test -f "$STAGED_DIR/$name.next"
done
for artifact in "$STAGED_DIR/"*.next; do
  cp "$artifact" "$DEPLOY_DIR/overrides/"
done

echo 'reviewed overrides staged as .next files; no live service was changed'
