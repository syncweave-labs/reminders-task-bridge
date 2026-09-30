#!/usr/bin/env bash
# Installs "미리알림 날짜.app" from the reviewed main commit into the user's
# Applications folder (Finder → 응용 프로그램 in the home folder).
set -euo pipefail

APP_NAME="미리알림 날짜.app"
EXECUTABLE="DuePicker"
BUNDLE_ID="com.icloud-reminders-google-sync.due-picker"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
INSTALL_DIR="${DUE_PICKER_INSTALL_DIR:-${HOME}/Applications}"
OPEN_AFTER=0
REVEAL_AFTER=0
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

usage() {
  cat <<'USAGE'
Usage:
  DEPLOY_EXPECTED_COMMIT=<full-reviewed-main-sha> \
    bash scripts/install-due-picker-app.sh [--applications-dir DIR] [--open] [--reveal]

Builds the app from the verified main checkout and replaces
~/Applications/미리알림 날짜.app in one step. The same release-source gate as
setup-new-mac.sh runs first, so a task branch, a dirty tree or an unpushed
commit is never installed.

Options:
  --applications-dir DIR  Install somewhere other than ~/Applications.
  --open                  Open the app after installing.
  --reveal                Show the installed app in Finder.
  -h, --help              Show this help.

The app saves every change as it is made, so a running copy is closed before
it is replaced.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --applications-dir)
      shift
      [ "$#" -gt 0 ] || die "--applications-dir requires a value."
      INSTALL_DIR="$1"
      ;;
    --open)
      OPEN_AFTER=1
      ;;
    --reveal)
      REVEAL_AFTER=1
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

[ "$(uname -s)" = "Darwin" ] || die "The app installs only on macOS."
bash "${SCRIPT_DIR}/check-release-source.sh" --source-dir "$SOURCE_DIR"
RELEASE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD | tr 'A-F' 'a-f')"

case "$INSTALL_DIR" in
  /*) ;;
  *) die "The applications directory must be an absolute path." ;;
esac
mkdir -p "$INSTALL_DIR"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
case "${INSTALL_DIR}/" in
  "${SOURCE_DIR}/"*) die "The applications directory must not be inside the source checkout." ;;
esac
TARGET="${INSTALL_DIR}/${APP_NAME}"
if [ -L "$TARGET" ]; then
  die "Install path is a symlink; remove it first: $TARGET"
fi
if [ -e "$TARGET" ]; then
  [ -d "$TARGET" ] || die "Install path exists and is not an app bundle: $TARGET"
  EXISTING_ID="$(plutil -extract CFBundleIdentifier raw -o - "${TARGET}/Contents/Info.plist" 2>/dev/null || true)"
  [ "$EXISTING_ID" = "$BUNDLE_ID" ] || die "Refusing to replace a different app at $TARGET (bundle id: ${EXISTING_ID:-unknown})."
fi

# Stage next to the target so the final step is a same-volume rename.
STAGING="$(mktemp -d "${INSTALL_DIR}/.due-picker-install.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
bash "${SCRIPT_DIR}/build-due-picker-app.sh" --output "$STAGING" --release-commit "$RELEASE_COMMIT"
BUILT="${STAGING}/${APP_NAME}"
codesign --verify --strict "$BUILT"
BUILT_COMMIT="$(plutil -extract DuePickerSourceCommit raw -o - "${BUILT}/Contents/Info.plist")"
[ "$BUILT_COMMIT" = "$RELEASE_COMMIT" ] || die "Built bundle records ${BUILT_COMMIT}, expected ${RELEASE_COMMIT}."

if pgrep -x "$EXECUTABLE" >/dev/null 2>&1; then
  printf '==> Closing the running 미리알림 날짜 before replacing it\n'
  pkill -TERM -x "$EXECUTABLE" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -x "$EXECUTABLE" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if pgrep -x "$EXECUTABLE" >/dev/null 2>&1; then
    die "미리알림 날짜 is still running. Quit it and run the installer again."
  fi
fi

printf '==> Installing %s\n' "$TARGET"
if [ -d "$TARGET" ]; then
  mv "$TARGET" "${STAGING}/previous.app"
fi
if ! mv "$BUILT" "$TARGET"; then
  if [ -d "${STAGING}/previous.app" ]; then
    mv "${STAGING}/previous.app" "$TARGET"
  fi
  die "Could not move the new app into place; the previous version was kept."
fi
codesign --verify --strict "$TARGET"
if [ -x "$LSREGISTER" ]; then
  "$LSREGISTER" -f "$TARGET" >/dev/null 2>&1 || true
fi

printf 'Installed: %s\n' "$TARGET"
printf 'Source commit: %s\n' "$RELEASE_COMMIT"
printf 'The first launch asks for Reminders access; allow it once.\n'
if [ "$REVEAL_AFTER" -eq 1 ]; then
  open -R "$TARGET"
fi
if [ "$OPEN_AFTER" -eq 1 ]; then
  open "$TARGET"
fi
