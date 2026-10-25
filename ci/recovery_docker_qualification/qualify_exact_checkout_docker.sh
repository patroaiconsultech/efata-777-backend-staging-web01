#!/usr/bin/env bash
# EFATA 777: offline-to-Railway, Docker qualification ONLY.
# Checkout exactly the CI-approved Git SHA, overlay ONLY reviewed candidate files.
# Never connects to Railway, never runs migrations, never commits or pushes.
set -Eeuo pipefail
umask 077

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SHA="b4165bb85762d8fd5a414eb846ec13a4cc97ad1b"
REPO_URL="https://github.com/patroaiconsultech/efata-777-backend-staging-web01.git"
IMAGE="efata777-recovery-qualification:review"

for command in bash git docker sha256sum python3; do
  command -v "$command" >/dev/null || { echo "STOP: missing $command"; exit 3; }
done
docker info >/dev/null 2>&1 || { echo "STOP: Docker daemon inaccessible"; exit 3; }

cd "$BASE_DIR"
sha256sum --check --status SHA256SUMS.txt || {
  echo "STOP: candidate package hash mismatch"; exit 4;
}

EVIDENCE_DIR="${EFATA_QUALIFICATION_EVIDENCE_DIR:-$BASE_DIR/evidence}"
mkdir -p "$EVIDENCE_DIR"
WORK_DIR="$(mktemp -d -t efata777-exact-qualification-XXXXXX)"
trap 'rm -rf -- "$WORK_DIR"' EXIT
WORK_TREE="$WORK_DIR/backend"

printf '%s\n' "QUALIFICATION_ONLY=TRUE" \
  "NO_RAILWAY_CONNECTION=TRUE" \
  "EXPECTED_GIT_SHA=$SOURCE_SHA" \
  "DOCKER_BUILD=NOT_STARTED" > "$EVIDENCE_DIR/summary.txt"

echo "CHECKOUT_EXACT_PINNED_GIT_SHA"
git init -q "$WORK_TREE"
git -C "$WORK_TREE" remote add origin "$REPO_URL"
# No push, no persistent access token and no branch movement.
git -C "$WORK_TREE" fetch --quiet --no-tags --depth=1 origin "$SOURCE_SHA"
git -C "$WORK_TREE" checkout --quiet --detach FETCH_HEAD
ACTUAL_SHA="$(git -C "$WORK_TREE" rev-parse HEAD)"
if [[ "$ACTUAL_SHA" != "$SOURCE_SHA" ]]; then
  echo "STOP: source commit mismatch"
  exit 4
fi
TREE_SHA="$(git -C "$WORK_TREE" rev-parse 'HEAD^{tree}')"
printf 'ACTUAL_GIT_SHA=%s\nGIT_TREE_SHA=%s\n' "$ACTUAL_SHA" "$TREE_SHA" >> "$EVIDENCE_DIR/summary.txt"

echo "VERIFY_PATCH_TARGETS_ARE_NEW_IN_THE_FROZEN_CHECKOUT"
while IFS='|' read -r source_path target_path; do
  [[ -n "$source_path" ]] || continue
  if [[ -e "$WORK_TREE/$target_path" ]]; then
    echo "STOP: checkout already contains candidate path $target_path"
    exit 4
  fi
  install -D -m 0644 "$BASE_DIR/$source_path" "$WORK_TREE/$target_path"
  printf 'CANDIDATE_SHA256 %s %s\\n' \
      "$(sha256sum "$WORK_TREE/$target_path" | cut -d' ' -f1)" "$target_path" >> "$EVIDENCE_DIR/summary.txt"
done <<'CANDIDATES'
railway_recovery_migrate_once.py|scripts/railway_recovery_migrate_once.py
test_railway_recovery_runner_real_backend.py|tests/test_railway_recovery_runner_real_backend.py
Dockerfile.recovery-qualification|Dockerfile.recovery-qualification
qualify_recovery_image_local.sh|scripts/qualify_recovery_image_local.sh
CANDIDATES

echo "BUILD_FROZEN_BACKEND_PLUS_CANDIDATE_DOCKER_IMAGE"
# Network is used only for GitHub, Docker base image and pinned PyPI hashes.
# No Railway secrets or DATABASE_URL are provided to Docker.
docker build --progress=plain --no-cache \
  --file "$WORK_TREE/Dockerfile.recovery-qualification" \
  --tag "$IMAGE" "$WORK_TREE" \
  > "$EVIDENCE_DIR/docker-build.log" 2>&1 || {
    echo "STOP: Docker build failed; examine evidence/docker-build.log"
    exit 5
  }
echo "DOCKER_BUILD=PASS" >> "$EVIDENCE_DIR/summary.txt"

echo "TEST_REAL_MODULE_OFFLINE"
docker run --rm --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --volume "$WORK_TREE/tests:/app/tests:ro" \
  --entrypoint python "$IMAGE" \
  -m unittest discover -s tests -p test_railway_recovery_runner_real_backend.py -v \
  > "$EVIDENCE_DIR/real-module-tests.log" 2>&1 || {
    echo "STOP: real-module test failure; see evidence/real-module-tests.log"
    exit 6
  }
grep -q "Ran 17 tests" "$EVIDENCE_DIR/real-module-tests.log" || {
  echo "STOP: unexpected number of tests"
  exit 6
}
grep -q "^OK$" "$EVIDENCE_DIR/real-module-tests.log" || {
  echo "STOP: unittest did not report OK"
  exit 6
}
echo "REAL_MODULE_TESTS=17_PASS" >> "$EVIDENCE_DIR/summary.txt"

echo "NEGATIVE_SMOKE_WITH_NO_NETWORK_NO_RAILWAY_CONTEXT"
set +e
docker run --rm --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --entrypoint python "$IMAGE" \
  scripts/railway_recovery_migrate_once.py \
  > "$EVIDENCE_DIR/negative-smoke.log" 2>&1
exitcode=$?
set -e
if [[ "$exitcode" != "2" ]]; then
  echo "STOP: unexpected negative-smoke exit $exitcode"
  exit 7
fi
grep -q '"status": "STOP_AND_PRESERVE"' "$EVIDENCE_DIR/negative-smoke.log" || {
  echo "STOP: fail-closed marker missing"
  exit 7
}
echo "NEGATIVE_SMOKE=PASS_EXIT_2_STOP_AND_PRESERVE" >> "$EVIDENCE_DIR/summary.txt"
echo "QUALIFICATION_RESULT=PASS_WITHOUT_RAILWAY" >> "$EVIDENCE_DIR/summary.txt"
echo "QUALIFICATION_PASS: results in $EVIDENCE_DIR"
