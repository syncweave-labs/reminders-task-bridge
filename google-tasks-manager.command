#!/usr/bin/env bash
set -euo pipefail

LAUNCHER_SOURCE="${BASH_SOURCE[0]}"
while [ -L "$LAUNCHER_SOURCE" ]; do
  LAUNCHER_DIR="$(cd "$(dirname "$LAUNCHER_SOURCE")" && pwd -P)"
  LAUNCHER_TARGET="$(readlink "$LAUNCHER_SOURCE")"
  case "$LAUNCHER_TARGET" in
    /*) LAUNCHER_SOURCE="$LAUNCHER_TARGET" ;;
    *) LAUNCHER_SOURCE="${LAUNCHER_DIR}/${LAUNCHER_TARGET}" ;;
  esac
done
RUNTIME_CURRENT="$(cd "$(dirname "$LAUNCHER_SOURCE")" && pwd -P)"
SYNC_SCRIPT="${RUNTIME_CURRENT}/icloud_reminders_google_sync.py"
PYTHON_BIN="${PYTHON_BIN:-}"

die() {
  printf 'Google Tasks 관리 도구를 시작할 수 없습니다: %s\n' "$*" >&2
  printf '검토된 main 릴리스를 setup-new-mac.sh로 다시 설치한 뒤 시도하세요.\n' >&2
  exit 1
}

if [ -z "$PYTHON_BIN" ]; then
  if [ -x /opt/homebrew/bin/python3 ]; then
    PYTHON_BIN="/opt/homebrew/bin/python3"
  elif [ -x /usr/local/bin/python3 ]; then
    PYTHON_BIN="/usr/local/bin/python3"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    die "Python 3을 찾지 못했습니다."
  fi
fi

[ -x "$PYTHON_BIN" ] || die "설정된 Python이 실행 가능하지 않습니다."
[ -f "$SYNC_SCRIPT" ] || die "안정 실행 릴리스를 찾지 못했습니다."

if [ "$#" -eq 0 ]; then
  set -- menu
fi

exec "$PYTHON_BIN" "$SYNC_SCRIPT" manage "$@"
