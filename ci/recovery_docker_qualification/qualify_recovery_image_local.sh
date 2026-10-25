#!/usr/bin/env bash
# ONLY local Docker qualification against a checkout of the full frozen backend.
# Does not contact Railway or run migrations; no DATABASE_URL values required.
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v docker >/dev/null || { echo "STOP: Docker unavailable"; exit 3; }
test -f Dockerfile.recovery-qualification || { echo "STOP: checkout missing runner Dockerfile"; exit 3; }
test -f scripts/schema_recovery_full_chain_ci.py || { echo "STOP: helper missing"; exit 3; }
test -f tests/test_railway_recovery_runner_real_backend.py || { echo "STOP: test missing"; exit 3; }

echo "BUILD_NO_RAILWAY_SECRETS"
docker build -f Dockerfile.recovery-qualification -t efata777-runner-qualification:review .
echo "TEST_REAL_HELPER_NETWORK_DISABLED"
docker run --rm --network none --read-only \
  -v "$PWD/tests:/app/tests:ro" \
  --entrypoint python efata777-runner-qualification:review \
  -m unittest discover -s tests -p test_railway_recovery_runner_real_backend.py -v
echo "ASSERT_DEFAULT_RUNNER_STOPS_NO_RAILWAY_CONTEXT"
set +e
output=$(docker run --rm --network none --read-only \
  --entrypoint python efata777-runner-qualification:review \
  scripts/railway_recovery_migrate_once.py 2>&1)
code=$?
set -e
# Never echo the output: it might someday contain more than intended.
test "$code" -eq 2 || { echo "STOP: wrong negative smoke exit=$code"; exit 4; }
grep -q '"status": "STOP_AND_PRESERVE"' <<< "$output" || {
  echo "STOP: no fail-closed marker"; exit 4;
}
echo "LOCAL_IMAGE_QUALIFICATION_PASS_NO_RAILWAY"
