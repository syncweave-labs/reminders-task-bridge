#!/usr/bin/env bash
# Builds "미리알림 날짜.app" from due-picker/ with the Command Line Tools
# (swiftc, iconutil, codesign); Xcode is not required.
set -euo pipefail

APP_NAME="미리알림 날짜"
EXECUTABLE="DuePicker"
MIN_MACOS="14.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
PROJECT_DIR="${SOURCE_DIR}/due-picker"
OUTPUT_DIR="${PROJECT_DIR}/build"
RELEASE_COMMIT=""
SIGN_IDENTITY="${DUE_PICKER_SIGN_IDENTITY:-}"
SNAPSHOT_DIR=""

usage() {
  cat <<'USAGE'
Usage:
  bash scripts/build-due-picker-app.sh [--output DIR] [--release-commit SHA] [--sign IDENTITY]
  bash scripts/build-due-picker-app.sh --snapshots DIR

Options:
  --output DIR           Directory that receives "미리알림 날짜.app" (default: due-picker/build).
  --release-commit SHA   Record the reviewed commit in the bundle (the installer passes it).
  --sign IDENTITY        Code-signing identity. Defaults to DUE_PICKER_SIGN_IDENTITY, then the
                         first "Apple Development" or "Developer ID Application" identity, then
                         ad-hoc ("-").
  --snapshots DIR        Render the window with built-in demo data (never real reminders) into
                         PNG files in DIR instead of building the app.
  -h, --help             Show this help.

A stable signing identity matters: macOS remembers the Reminders permission for
the signed identity, so an ad-hoc build asks again after every rebuild.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)
      shift
      [ "$#" -gt 0 ] || die "--output requires a value."
      OUTPUT_DIR="$1"
      ;;
    --release-commit)
      shift
      [ "$#" -gt 0 ] || die "--release-commit requires a value."
      RELEASE_COMMIT="$1"
      ;;
    --sign)
      shift
      [ "$#" -gt 0 ] || die "--sign requires a value."
      SIGN_IDENTITY="$1"
      ;;
    --snapshots)
      shift
      [ "$#" -gt 0 ] || die "--snapshots requires a value."
      SNAPSHOT_DIR="$1"
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

[ "$(uname -s)" = "Darwin" ] || die "The app builds only on macOS."
for tool in swiftc iconutil codesign plutil ditto; do
  command -v "$tool" >/dev/null 2>&1 || die "Missing required tool: $tool (install the Xcode Command Line Tools)."
done
if [ -n "$RELEASE_COMMIT" ]; then
  case "$RELEASE_COMMIT" in
    *[!0-9a-f]*) die "--release-commit must be a lowercase hexadecimal commit SHA." ;;
  esac
  [ "${#RELEASE_COMMIT}" -eq 40 ] || die "--release-commit must be the full 40-character SHA."
fi

ARCH="$(uname -m)"
TARGET="${ARCH}-apple-macos${MIN_MACOS}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/due-picker-build.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

SOURCES=(
  "${PROJECT_DIR}/Sources/DueCore.swift"
  "${PROJECT_DIR}/Sources/ReminderStore.swift"
  "${PROJECT_DIR}/Sources/AppModel.swift"
  "${PROJECT_DIR}/Sources/Views.swift"
)
SWIFT_FLAGS=(-swift-version 5 -parse-as-library -target "$TARGET")

if [ -n "$SNAPSHOT_DIR" ]; then
  mkdir -p "$SNAPSHOT_DIR"
  swiftc "${SWIFT_FLAGS[@]}" -Onone \
    "${SOURCES[@]}" "${PROJECT_DIR}/Tools/DemoBackend.swift" "${PROJECT_DIR}/Tools/Snapshot.swift" \
    -o "${WORK_DIR}/snapshot"
  "${WORK_DIR}/snapshot" "$SNAPSHOT_DIR"
  exit 0
fi

printf '==> Compiling %s (%s)\n' "$EXECUTABLE" "$TARGET"
swiftc "${SWIFT_FLAGS[@]}" -O -module-name DuePicker \
  "${SOURCES[@]}" "${PROJECT_DIR}/Sources/App.swift" \
  -o "${WORK_DIR}/${EXECUTABLE}"

printf '==> Drawing the app icon\n'
swiftc -O -target "$TARGET" "${PROJECT_DIR}/Tools/MakeIcon.swift" -o "${WORK_DIR}/make-icon"
"${WORK_DIR}/make-icon" "${WORK_DIR}/AppIcon.iconset"
iconutil -c icns "${WORK_DIR}/AppIcon.iconset" -o "${WORK_DIR}/AppIcon.icns"

printf '==> Assembling the bundle\n'
BUNDLE="${WORK_DIR}/${APP_NAME}.app"
mkdir -p "${BUNDLE}/Contents/MacOS" "${BUNDLE}/Contents/Resources/ko.lproj"
cp "${WORK_DIR}/${EXECUTABLE}" "${BUNDLE}/Contents/MacOS/${EXECUTABLE}"
chmod 0755 "${BUNDLE}/Contents/MacOS/${EXECUTABLE}"
cp "${PROJECT_DIR}/Info.plist" "${BUNDLE}/Contents/Info.plist"
cp "${WORK_DIR}/AppIcon.icns" "${BUNDLE}/Contents/Resources/AppIcon.icns"
printf 'APPL????' >"${BUNDLE}/Contents/PkgInfo"
printf '"CFBundleDisplayName" = "%s";\n"CFBundleName" = "%s";\n' "$APP_NAME" "$APP_NAME" \
  >"${BUNDLE}/Contents/Resources/ko.lproj/InfoPlist.strings"
if git -C "$SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  BUILD_NUMBER="$(git -C "$SOURCE_DIR" rev-list --count HEAD 2>/dev/null || printf '1')"
  plutil -replace CFBundleVersion -string "$BUILD_NUMBER" "${BUNDLE}/Contents/Info.plist"
fi
if [ -n "$RELEASE_COMMIT" ]; then
  plutil -replace DuePickerSourceCommit -string "$RELEASE_COMMIT" "${BUNDLE}/Contents/Info.plist"
  cat >"${BUNDLE}/Contents/Resources/release-source.txt" <<RELEASE
repository=syncweave-labs/reminders-task-bridge
commit=${RELEASE_COMMIT}
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RELEASE
fi
plutil -lint "${BUNDLE}/Contents/Info.plist" >/dev/null

if [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | awk '/"(Apple Development|Developer ID Application):/ {print $2; exit}')"
fi
if [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY="-"
  printf 'WARN: no signing identity found; signing ad hoc. macOS will ask for Reminders access again after each rebuild.\n' >&2
fi

printf '==> Signing\n'
# Hardened runtime keeps other processes from injecting code into an app that
# holds the Reminders permission; EventKit needs the calendars entitlement under it.
codesign --force --timestamp=none --options runtime \
  --entitlements "${PROJECT_DIR}/DuePicker.entitlements" \
  --sign "$SIGN_IDENTITY" "$BUNDLE"
codesign --verify --strict "$BUNDLE"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
rm -rf "${OUTPUT_DIR:?}/${APP_NAME}.app"
ditto "$BUNDLE" "${OUTPUT_DIR}/${APP_NAME}.app"
codesign --verify --strict "${OUTPUT_DIR}/${APP_NAME}.app"
printf 'Built %s\n' "${OUTPUT_DIR}/${APP_NAME}.app"
