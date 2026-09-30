#!/usr/bin/env bash
# Runs the 미리알림 날짜 tests. The date logic runs anywhere Swift does (CI
# runs it on Linux); the EventKit and app-model tests need macOS. Neither
# reads or writes real reminders: EventKit tests edit unsaved in-memory objects.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)/due-picker"
SWIFTC="${SWIFTC:-$(command -v swiftc || true)}"
if [ -z "$SWIFTC" ] && [ -x /usr/share/swift/usr/bin/swiftc ]; then
  SWIFTC=/usr/share/swift/usr/bin/swiftc
fi
[ -n "$SWIFTC" ] || { printf 'ERROR: swiftc not found.\n' >&2; exit 1; }

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/due-picker-tests.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

"$SWIFTC" -swift-version 5 -Onone \
  "${PROJECT_DIR}/Sources/DueCore.swift" \
  "${PROJECT_DIR}/Tests/CoreTests/main.swift" \
  -o "${WORK_DIR}/core-tests"
"${WORK_DIR}/core-tests"

if [ "$(uname -s)" = "Darwin" ]; then
  "$SWIFTC" -swift-version 5 -Onone -parse-as-library -target "$(uname -m)-apple-macos14.0" \
    "${PROJECT_DIR}/Sources/DueCore.swift" \
    "${PROJECT_DIR}/Sources/ReminderStore.swift" \
    "${PROJECT_DIR}/Sources/AppModel.swift" \
    "${PROJECT_DIR}/Tools/DemoBackend.swift" \
    "${PROJECT_DIR}/Tests/AppTests/AppTests.swift" \
    -o "${WORK_DIR}/app-tests"
  "${WORK_DIR}/app-tests"
else
  printf 'SKIP: EventKit and app-model tests need macOS.\n'
fi
