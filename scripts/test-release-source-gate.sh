#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
GATE="${SCRIPT_DIR}/check-release-source.sh"
EXPECTED_URL="https://github.com/syncweave-labs/reminders-task-bridge.git"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/icloud-sync-release-gate.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

REMOTE_DIR="${TMP_ROOT}/remote.git"
SOURCE_DIR="${TMP_ROOT}/source"
OUTPUT_PATH="${TMP_ROOT}/gate-output.txt"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

expect_failure() {
  local label="$1"
  shift
  if "$@" >"$OUTPUT_PATH" 2>&1; then
    fail "$label unexpectedly passed"
  fi
  printf 'PASS (blocked): %s\n' "$label"
}

git init --bare "$REMOTE_DIR" >/dev/null
git init "$SOURCE_DIR" >/dev/null
git -C "$SOURCE_DIR" config user.name "Release Gate Test"
git -C "$SOURCE_DIR" config user.email "release-gate@example.invalid"
git -C "$SOURCE_DIR" branch -M main
printf 'source\n' >"${SOURCE_DIR}/tracked.txt"
git -C "$SOURCE_DIR" add tracked.txt
git -C "$SOURCE_DIR" commit -m "Initial source" >/dev/null
git -C "$SOURCE_DIR" remote add origin "$EXPECTED_URL"
git -C "$SOURCE_DIR" config "url.file://${REMOTE_DIR}.insteadOf" "$EXPECTED_URL"
git -C "$SOURCE_DIR" push -u origin main >/dev/null

HEAD_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
DEPLOY_EXPECTED_COMMIT="$HEAD_COMMIT" "$GATE" --source-dir "$SOURCE_DIR" >/dev/null
printf 'PASS: clean reviewed main\n'

expect_failure "missing expected commit" \
  env -u DEPLOY_EXPECTED_COMMIT "$GATE" --source-dir "$SOURCE_DIR"

printf 'dirty\n' >>"${SOURCE_DIR}/tracked.txt"
expect_failure "dirty worktree" \
  env DEPLOY_EXPECTED_COMMIT="$HEAD_COMMIT" "$GATE" --source-dir "$SOURCE_DIR"
expect_failure "override without reason" \
  env DEPLOY_EXPECTED_COMMIT="$HEAD_COMMIT" DEPLOY_GIT_ALLOW_DIRTY=1 "$GATE" --source-dir "$SOURCE_DIR"
DEPLOY_EXPECTED_COMMIT="$HEAD_COMMIT" \
  DEPLOY_GIT_ALLOW_DIRTY=1 \
  DEPLOY_GIT_OVERRIDE_REASON="release gate self-test" \
  "$GATE" --source-dir "$SOURCE_DIR" >/dev/null
printf 'PASS: audited dirty override\n'
printf 'source\n' >"${SOURCE_DIR}/tracked.txt"

git -C "$SOURCE_DIR" switch -c feature >/dev/null
expect_failure "non-main branch" \
  env DEPLOY_EXPECTED_COMMIT="$HEAD_COMMIT" "$GATE" --source-dir "$SOURCE_DIR"
DEPLOY_EXPECTED_COMMIT="$HEAD_COMMIT" \
  DEPLOY_GIT_ALLOW_NON_MAIN=1 \
  DEPLOY_GIT_OVERRIDE_REASON="release gate self-test" \
  "$GATE" --source-dir "$SOURCE_DIR" >/dev/null
printf 'PASS: audited non-main override\n'
git -C "$SOURCE_DIR" switch main >/dev/null

printf 'second\n' >>"${SOURCE_DIR}/tracked.txt"
git -C "$SOURCE_DIR" add tracked.txt
git -C "$SOURCE_DIR" commit -m "Unpushed source" >/dev/null
UNPUSHED_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
expect_failure "unpushed commit" \
  env DEPLOY_EXPECTED_COMMIT="$UNPUSHED_COMMIT" "$GATE" --source-dir "$SOURCE_DIR"
DEPLOY_EXPECTED_COMMIT="$UNPUSHED_COMMIT" \
  DEPLOY_GIT_ALLOW_UNPUSHED=1 \
  DEPLOY_GIT_OVERRIDE_REASON="release gate self-test" \
  "$GATE" --source-dir "$SOURCE_DIR" >/dev/null
printf 'PASS: audited unpushed override\n'
git -C "$SOURCE_DIR" push origin main >/dev/null

expect_failure "expected commit mismatch" \
  env DEPLOY_EXPECTED_COMMIT="0000000000000000000000000000000000000000" "$GATE" --source-dir "$SOURCE_DIR"

git -C "$SOURCE_DIR" remote set-url origin "https://github.com/not-the-owner/not-this-repo.git"
expect_failure "unexpected origin" \
  env DEPLOY_EXPECTED_COMMIT="$UNPUSHED_COMMIT" "$GATE" --source-dir "$SOURCE_DIR"
git -C "$SOURCE_DIR" remote set-url origin "$EXPECTED_URL"

BUNDLE_DIR="${TMP_ROOT}/bundle"
mkdir -p "$BUNDLE_DIR"
printf 'payload\n' >"${BUNDLE_DIR}/payload.txt"
cat >"${BUNDLE_DIR}/release-source.txt" <<MANIFEST
repository=syncweave-labs/reminders-task-bridge
branch=main
commit=${UNPUSHED_COMMIT}
MANIFEST
(
  cd "$BUNDLE_DIR"
  shasum -a 256 payload.txt release-source.txt >release-source.sha256
)
expect_failure "implicit bundle bootstrap" \
  env DEPLOY_EXPECTED_COMMIT="$UNPUSHED_COMMIT" "$GATE" --source-dir "$BUNDLE_DIR" --bootstrap-manifest release-source.txt
expect_failure "bundle override without reason" \
  env DEPLOY_EXPECTED_COMMIT="$UNPUSHED_COMMIT" DEPLOY_GIT_ALLOW_BOOTSTRAP=1 "$GATE" --source-dir "$BUNDLE_DIR" --bootstrap-manifest release-source.txt
DEPLOY_EXPECTED_COMMIT="$UNPUSHED_COMMIT" \
  DEPLOY_GIT_ALLOW_BOOTSTRAP=1 \
  DEPLOY_GIT_OVERRIDE_REASON="release gate self-test" \
  "$GATE" --source-dir "$BUNDLE_DIR" --bootstrap-manifest release-source.txt >/dev/null
printf 'PASS: verified bootstrap bundle\n'

printf 'tampered\n' >"${BUNDLE_DIR}/payload.txt"
expect_failure "tampered bootstrap bundle" \
  env DEPLOY_EXPECTED_COMMIT="$UNPUSHED_COMMIT" DEPLOY_GIT_ALLOW_BOOTSTRAP=1 DEPLOY_GIT_OVERRIDE_REASON="release gate self-test" "$GATE" --source-dir "$BUNDLE_DIR" --bootstrap-manifest release-source.txt

printf 'Release source gate self-test passed.\n'
