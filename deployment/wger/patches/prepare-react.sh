#!/bin/bash
# Stage reviewed release inputs locally against the configured Docker endpoint.
# Outputs .next files only; release-web.sh owns the separate release window.
# --backend-image builds the pinned backend source image and stages its provenance;
# the default mode stages the corresponding-source record for that image.
set -euo pipefail

PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$PATCH_DIR/.." && pwd)
DOCKER_HOST=${WGER_DOCKER_HOST-unix://$HOME/.colima/default/docker.sock}
BACKEND_REPO=https://github.com/Cub-HQ/wger-backend
BACKEND_COMMIT=ea6244abbd1512b2d731c37978e38933160b2b5b
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
BUILD_DIR=$(mktemp -d)
STAGED_DIR="$BUILD_DIR/staged"
mkdir -p "$STAGED_DIR"
trap 'rm -rf "$BUILD_DIR"' EXIT
mkdir -p "$DEPLOY_DIR/overrides"

# Corresponding source for what the image actually serves: the backend commit it was built from
# and the frontend commit its committed react-components package was built from.
cat > "$STAGED_DIR/corresponding-source.json.next" <<EOF
{"license":"AGPL-3.0","server":{"repository":"$BACKEND_REPO","commit":"$BACKEND_COMMIT","upstream":"${BACKEND_UPSTREAM% *}","upstream_commit":"${BACKEND_UPSTREAM#* }"},"frontend":{"repository":"$FRONTEND_REPO","commit":"$FRONTEND_COMMIT","upstream":"${FRONTEND_UPSTREAM% *}","upstream_commit":"${FRONTEND_UPSTREAM#* }"}}
EOF

test -f "$STAGED_DIR/corresponding-source.json.next"
for artifact in "$STAGED_DIR/"*.next; do
  cp "$artifact" "$DEPLOY_DIR/overrides/"
done

echo 'reviewed overrides staged as .next files; no live service was changed'
