#!/usr/bin/env bash
set -euo pipefail

APP_DIR_NAME="icloud-reminders-google-sync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_OUTPUT_DIR="${HOME}/Desktop"
OUTPUT_DIR="${OUTPUT_DIR:-$DEFAULT_OUTPUT_DIR}"
INCLUDE_CREDENTIALS=1

usage() {
  cat <<'USAGE'
Usage:
  bash make-migration-bundle.sh [options]

Options:
  --output-dir DIR       Directory where the migration archive will be created. Default: ~/Desktop.
  --without-credentials  Do not include credentials.json in the archive.
  -h, --help             Show this help.

What this creates:
  A tar.gz archive that can be moved to a new Mac. Extract it, then run:

    cd ~/apps/icloud-reminders-google-sync
    # Use the commit and bootstrap command written in NEW_MAC_SETUP.txt.

The archive intentionally does not include ADC login credentials, sync state, tokens, or logs.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-dir)
      shift
      [ "$#" -gt 0 ] || die "--output-dir requires a value."
      OUTPUT_DIR="$1"
      ;;
    --without-credentials)
      INCLUDE_CREDENTIALS=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done

[ -d "$SCRIPT_DIR" ] || die "Project directory not found: $SCRIPT_DIR"
RELEASE_GATE="${SCRIPT_DIR}/scripts/check-release-source.sh"
[ -f "$RELEASE_GATE" ] || die "Release-source gate not found: $RELEASE_GATE"
bash "$RELEASE_GATE" --source-dir "$SCRIPT_DIR"
SOURCE_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"

mkdir -p "$OUTPUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/icloud-reminders-google-sync-migration.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

BUNDLE_ROOT="${WORK_DIR}/${APP_DIR_NAME}"
mkdir -p "$BUNDLE_ROOT"

copy_required() {
  local name="$1"
  [ -f "${SCRIPT_DIR}/${name}" ] || die "Required file is missing: ${SCRIPT_DIR}/${name}"
  mkdir -p "$(dirname "${BUNDLE_ROOT}/${name}")"
  cp "${SCRIPT_DIR}/${name}" "${BUNDLE_ROOT}/${name}"
}

copy_required "icloud_reminders_google_sync.py"
copy_required "RemindersExport.swift"
copy_required "RemindersApply.swift"
copy_required "google-tasks-manager.command"
copy_required "setup-new-mac.sh"
copy_required "make-migration-bundle.sh"
copy_required "README.md"
copy_required "scripts/check-release-source.sh"

chmod +x \
  "${BUNDLE_ROOT}/setup-new-mac.sh" \
  "${BUNDLE_ROOT}/make-migration-bundle.sh" \
  "${BUNDLE_ROOT}/google-tasks-manager.command" \
  "${BUNDLE_ROOT}/scripts/check-release-source.sh" || true

cat >"${BUNDLE_ROOT}/release-source.txt" <<MANIFEST
repository=syncweave-labs/reminders-task-bridge
branch=main
commit=${SOURCE_COMMIT}
MANIFEST

(
  cd "$BUNDLE_ROOT"
  shasum -a 256 \
    icloud_reminders_google_sync.py \
    RemindersExport.swift \
    RemindersApply.swift \
    google-tasks-manager.command \
    setup-new-mac.sh \
    make-migration-bundle.sh \
    README.md \
    scripts/check-release-source.sh \
    release-source.txt \
    >release-source.sha256
)

if [ "$INCLUDE_CREDENTIALS" -eq 1 ]; then
  if [ -f "${SCRIPT_DIR}/credentials.json" ]; then
    cp "${SCRIPT_DIR}/credentials.json" "${BUNDLE_ROOT}/credentials.json"
  elif [ -f "${HOME}/.config/icloud-reminders-google-sync/credentials.json" ]; then
    cp "${HOME}/.config/icloud-reminders-google-sync/credentials.json" "${BUNDLE_ROOT}/credentials.json"
  else
    die "credentials.json was not found. Put it next to this script or at ~/.config/icloud-reminders-google-sync/credentials.json, or rerun with --without-credentials."
  fi
  chmod 600 "${BUNDLE_ROOT}/credentials.json" || true
fi

cat > "${BUNDLE_ROOT}/NEW_MAC_SETUP.txt" <<TXT
새 Mac 설치 순서

1. 새 Mac에서 같은 iCloud 계정으로 로그인한다.
2. System Settings에서 iCloud Reminders 동기화가 켜져 있는지 확인한다.
3. 이 압축 파일을 새 Mac으로 옮긴다.
4. 압축을 푼 폴더를 원하는 위치에 둔다. 권장 위치:

   ~/apps/icloud-reminders-google-sync

5. Terminal에서 실행한다.

   cd ~/apps/icloud-reminders-google-sync
   DEPLOY_EXPECTED_COMMIT=${SOURCE_COMMIT} \\
     DEPLOY_GIT_ALLOW_BOOTSTRAP=1 \\
     DEPLOY_GIT_OVERRIDE_REASON='first-machine verified migration bundle' \\
     bash setup-new-mac.sh --bootstrap-from-bundle

참고:
- release-source.sha256으로 실행 코드가 번들 생성 이후 바뀌지 않았는지 검사한다.
- credentials.json이 이 폴더 안에 있으면 setup-new-mac.sh가 설정 폴더로 복사한다.
- Google 로그인과 Apple Reminders 권한 승인은 새 Mac에서 다시 진행해야 한다.
- 이 압축 파일에는 Google OAuth client JSON이 포함될 수 있으므로 다른 사람에게 공유하지 말 것.
- application_default_credentials.json, state.json, token.json은 포함하지 않는다.
TXT

ARCHIVE_NAME="${APP_DIR_NAME}-migration-${STAMP}.tar.gz"
ARCHIVE_PATH="${OUTPUT_DIR%/}/${ARCHIVE_NAME}"

tar -C "$WORK_DIR" -czf "$ARCHIVE_PATH" "$APP_DIR_NAME"
chmod 600 "$ARCHIVE_PATH" || true

printf 'Created migration archive:\n%s\n' "$ARCHIVE_PATH"
if [ "$INCLUDE_CREDENTIALS" -eq 1 ]; then
  printf 'Included credentials.json. Keep this archive private.\n'
else
  printf 'credentials.json was not included. Add it on the new Mac before running setup-new-mac.sh.\n'
fi
