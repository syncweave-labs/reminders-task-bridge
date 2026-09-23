#!/usr/bin/env bash
set -euo pipefail

LABEL="com.icloud-reminders-google-sync"
GCP_PROJECT="${GCP_PROJECT:-}"
ASSUME_YES=0
SKIP_GOOGLE_LOGIN=0
SKIP_FIRST_SYNC=0
SKIP_TOOL_INSTALL=0
BOOTSTRAP_FROM_BUNDLE=0
PYTHON_BIN="${PYTHON_BIN:-}"

export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/google-cloud-sdk/bin:${PATH}"

usage() {
  cat <<'USAGE'
Usage:
  bash setup-new-mac.sh [options]

Options:
  --yes                 Do not stop for confirmation when a dry-run looks safe.
  --project PROJECT_ID  Google Cloud project ID to configure and enable Tasks API for.
  --python-bin PATH     Python executable to use in LaunchAgent.
  --skip-google-login   Do not run Google ADC login.
  --skip-first-sync     Do not run the first safe sync.
  --skip-tool-install   Do not try Homebrew installs for missing tools.
  --bootstrap-from-bundle
                        Install from a verified first-machine migration bundle.
  -h, --help            Show this help.

Before running on a new Mac:
  1. Copy this project folder to the new Mac.
  2. Put the Google OAuth Desktop client JSON as credentials.json next to this script,
     or at ~/.config/icloud-reminders-google-sync/credentials.json.
  3. Sign in to iCloud and enable Reminders sync.

Normal Git checkout:
  DEPLOY_EXPECTED_COMMIT=<full-reviewed-main-sha> bash setup-new-mac.sh

Verified first-machine bundle:
  DEPLOY_EXPECTED_COMMIT=<sha-from-NEW_MAC_SETUP.txt> \
    DEPLOY_GIT_ALLOW_BOOTSTRAP=1 \
    DEPLOY_GIT_OVERRIDE_REASON='first-machine verified migration bundle' \
    bash setup-new-mac.sh --bootstrap-from-bundle
USAGE
}

info() {
  printf '\n==> %s\n' "$*"
}

warn() {
  printf '\nWARN: %s\n' "$*" >&2
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

confirm() {
  local prompt="$1"
  local answer

  if [ "$ASSUME_YES" -eq 1 ]; then
    return 0
  fi

  printf '%s [y/N] ' "$prompt"
  read -r answer
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes)
      ASSUME_YES=1
      ;;
    --project)
      shift
      [ "$#" -gt 0 ] || die "--project requires a value."
      GCP_PROJECT="$1"
      ;;
    --python-bin)
      shift
      [ "$#" -gt 0 ] || die "--python-bin requires a value."
      PYTHON_BIN="$1"
      ;;
    --skip-google-login)
      SKIP_GOOGLE_LOGIN=1
      ;;
    --skip-first-sync)
      SKIP_FIRST_SYNC=1
      ;;
    --skip-tool-install)
      SKIP_TOOL_INSTALL=1
      ;;
    --bootstrap-from-bundle)
      BOOTSTRAP_FROM_BUNDLE=1
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RELEASE_GATE="${SCRIPT_DIR}/scripts/check-release-source.sh"
SOURCE_SYNC_SCRIPT="${SCRIPT_DIR}/icloud_reminders_google_sync.py"
SOURCE_EXPORTER="${SCRIPT_DIR}/RemindersExport.swift"
SOURCE_APPLIER="${SCRIPT_DIR}/RemindersApply.swift"
SOURCE_MANAGER="${SCRIPT_DIR}/google-tasks-manager.command"

[ -f "$RELEASE_GATE" ] || die "Missing release-source gate: $RELEASE_GATE"
if [ "$BOOTSTRAP_FROM_BUNDLE" -eq 1 ]; then
  bash "$RELEASE_GATE" \
    --source-dir "$SCRIPT_DIR" \
    --bootstrap-manifest release-source.txt
  RELEASE_COMMIT="$(sed -n 's/^commit=//p' "${SCRIPT_DIR}/release-source.txt" | head -n 1)"
else
  bash "$RELEASE_GATE" --source-dir "$SCRIPT_DIR"
  RELEASE_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
fi

RUNTIME_ROOT="${SYNC_RUNTIME_ROOT:-${HOME}/.local/share/icloud-reminders-google-sync}"
case "$RUNTIME_ROOT" in
  /*) ;;
  *) die "SYNC_RUNTIME_ROOT must be an absolute, stable path." ;;
esac
case "${RUNTIME_ROOT}/" in
  "${SCRIPT_DIR}/"*) die "SYNC_RUNTIME_ROOT must not be inside the source checkout or worktree." ;;
esac

RELEASE_ID="$RELEASE_COMMIT"
if [ "${DEPLOY_GIT_ALLOW_DIRTY:-0}" -eq 1 ]; then
  RELEASE_ID="${RELEASE_COMMIT}-override-$(date +%Y%m%d%H%M%S)"
fi
RUNTIME_RELEASES_DIR="${RUNTIME_ROOT}/releases"
RUNTIME_RELEASE_DIR="${RUNTIME_RELEASES_DIR}/${RELEASE_ID}"
RUNTIME_CURRENT="${RUNTIME_ROOT}/current"
SYNC_SCRIPT="${RUNTIME_CURRENT}/icloud_reminders_google_sync.py"
EXPORTER="${RUNTIME_CURRENT}/RemindersExport.swift"
APPLIER="${RUNTIME_CURRENT}/RemindersApply.swift"
MANAGER="${RUNTIME_CURRENT}/google-tasks-manager.command"
CONFIG_DIR="${HOME}/.config/icloud-reminders-google-sync"
CONFIG_PATH="${CONFIG_DIR}/config.json"
CREDENTIALS_PATH="${CONFIG_DIR}/credentials.json"
ADC_PATH="${HOME}/.config/gcloud/application_default_credentials.json"
STATE_PATH="${CONFIG_DIR}/state.json"
TOKEN_PATH="${CONFIG_DIR}/token.json"
STATUS_PATH="${CONFIG_DIR}/status.json"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
MANAGER_LAUNCHER="${HOME}/Applications/Google Tasks 동기화 관리.command"
LOG_DIR="${HOME}/Library/Logs/icloud-reminders-google-sync"
STDOUT_LOG="${LOG_DIR}/sync.out.log"
STDERR_LOG="${LOG_DIR}/sync.err.log"
LEGACY_STDOUT_LOG="/tmp/icloud-reminders-google-sync.out.log"
LEGACY_STDERR_LOG="/tmp/icloud-reminders-google-sync.err.log"
LOG_MAX_BYTES="${ICLOUD_SYNC_LOG_MAX_BYTES:-5242880}"

prepare_runtime_logs() {
  local log_path
  local log_size
  local legacy_path

  case "$LOG_MAX_BYTES" in
    ''|*[!0-9]*) die "ICLOUD_SYNC_LOG_MAX_BYTES must be a non-negative integer." ;;
  esac

  # Reminder and task titles end up in these logs, so they must never live in a
  # world-writable, world-readable directory. launchd reopens the log paths by
  # name on every restart and recreates a missing file with its own default
  # mode, so the per-file chmod below cannot be the only protection: the
  # containing directory carries it instead.
  if [ -L "$LOG_DIR" ]; then
    die "Refusing to use a symlinked log directory: ${LOG_DIR}"
  fi
  mkdir -p "$LOG_DIR"
  chmod 700 "$LOG_DIR"

  for log_path in "$STDOUT_LOG" "$STDERR_LOG"; do
    if [ -L "$log_path" ]; then
      die "Refusing to write the LaunchAgent log through a symlink: ${log_path}"
    fi
    if [ -f "$log_path" ] && [ "$LOG_MAX_BYTES" -gt 0 ]; then
      log_size="$(wc -c <"$log_path" | tr -d ' ')"
      if [ "$log_size" -ge "$LOG_MAX_BYTES" ]; then
        rm -f "${log_path}.1"
        mv "$log_path" "${log_path}.1"
        chmod 600 "${log_path}.1"
      fi
    fi
    if [ ! -e "$log_path" ]; then
      (umask 077 && : >"$log_path")
    fi
    chmod 600 "$log_path"
  done

  # Earlier installs pointed the LaunchAgent at /tmp, where launchd left the
  # files world-readable. Retire that history instead of leaving it behind.
  for legacy_path in "$LEGACY_STDOUT_LOG" "$LEGACY_STDERR_LOG"; do
    if [ -f "$legacy_path" ] && [ ! -L "$legacy_path" ]; then
      info "Removing legacy world-readable log: ${legacy_path}"
      rm -f "$legacy_path" "${legacy_path}.1"
    fi
  done
}

install_runtime_release() {
  local staging_dir="${RUNTIME_RELEASES_DIR}/.${RELEASE_ID}.staging.$$"
  local name
  local override_reason

  info "Installing immutable runtime release: ${RUNTIME_RELEASE_DIR}"
  mkdir -p "$RUNTIME_RELEASES_DIR"

  if [ -e "$RUNTIME_RELEASE_DIR" ]; then
    [ -d "$RUNTIME_RELEASE_DIR" ] || die "Runtime release path is not a directory: $RUNTIME_RELEASE_DIR"
    for name in icloud_reminders_google_sync.py RemindersExport.swift RemindersApply.swift google-tasks-manager.command; do
      cmp -s "${SCRIPT_DIR}/${name}" "${RUNTIME_RELEASE_DIR}/${name}" || \
        die "Existing runtime release differs from reviewed source: ${RUNTIME_RELEASE_DIR}/${name}"
    done
  else
    mkdir "$staging_dir"
    if ! cp \
      "$SOURCE_SYNC_SCRIPT" \
      "$SOURCE_EXPORTER" \
      "$SOURCE_APPLIER" \
      "$SOURCE_MANAGER" \
      "$staging_dir/"; then
      rm -rf "$staging_dir"
      die "Could not stage runtime release."
    fi
    chmod 0555 "${staging_dir}/icloud_reminders_google_sync.py"
    chmod 0555 "${staging_dir}/google-tasks-manager.command"
    chmod 0444 "${staging_dir}/RemindersExport.swift" "${staging_dir}/RemindersApply.swift"
    override_reason="$(printf '%s' "${DEPLOY_GIT_OVERRIDE_REASON:-}" | tr '\r\n' '  ')"
    cat >"${staging_dir}/release-source.txt" <<RELEASE
repository=syncweave-labs/reminders-task-bridge
commit=${RELEASE_COMMIT}
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
override_reason=${override_reason}
RELEASE
    chmod 0444 "${staging_dir}/release-source.txt"
    mv "$staging_dir" "$RUNTIME_RELEASE_DIR"
  fi

  if [ -e "$RUNTIME_CURRENT" ] && [ ! -L "$RUNTIME_CURRENT" ]; then
    die "Runtime current path exists but is not a symlink: $RUNTIME_CURRENT"
  fi
  ln -sfn "releases/${RELEASE_ID}" "$RUNTIME_CURRENT"
  [ -f "$SYNC_SCRIPT" ] || die "Runtime release activation failed: $SYNC_SCRIPT"
}

install_management_launcher() {
  info "Installing Google Tasks management launcher: ${MANAGER_LAUNCHER}"
  [ -f "$MANAGER" ] || die "Runtime management command is missing: $MANAGER"
  mkdir -p "$(dirname "$MANAGER_LAUNCHER")"
  if [ -e "$MANAGER_LAUNCHER" ] && [ ! -L "$MANAGER_LAUNCHER" ]; then
    die "Management launcher path already exists and is not a symlink: $MANAGER_LAUNCHER"
  fi
  ln -sfn "$MANAGER" "$MANAGER_LAUNCHER"
}

install_if_missing() {
  local command_name="$1"
  local install_command="$2"

  if have "$command_name"; then
    return 0
  fi

  if [ "$SKIP_TOOL_INSTALL" -eq 1 ]; then
    die "$command_name is missing. Install it, then run this script again."
  fi

  if ! have brew; then
    die "$command_name is missing and Homebrew is not installed. Install Homebrew, then run this script again."
  fi

  info "Installing missing tool: ${command_name}"
  eval "$install_command"
  have "$command_name" || die "$command_name is still unavailable after install."
}

find_python() {
  if [ -n "$PYTHON_BIN" ]; then
    [ -x "$PYTHON_BIN" ] || die "Configured Python is not executable: $PYTHON_BIN"
    return 0
  fi

  if [ -x /opt/homebrew/bin/python3 ]; then
    PYTHON_BIN="/opt/homebrew/bin/python3"
    return 0
  fi

  if [ -x /usr/local/bin/python3 ]; then
    PYTHON_BIN="/usr/local/bin/python3"
    return 0
  fi

  if have python3; then
    PYTHON_BIN="$(command -v python3)"
    return 0
  fi

  PYTHON_BIN=""
  return 1
}

copy_credentials_if_needed() {
  local candidate

  if [ -f "$CREDENTIALS_PATH" ]; then
    chmod 600 "$CREDENTIALS_PATH" || true
    return 0
  fi

  for candidate in \
    "${SCRIPT_DIR}/credentials.json" \
    "${SCRIPT_DIR}/client_secret.json" \
    "${SCRIPT_DIR}"/client_secret_*.json \
    "${SCRIPT_DIR}"/*.apps.googleusercontent.com.json
  do
    if [ -f "$candidate" ]; then
      info "Copying OAuth client JSON from ${candidate}"
      cp "$candidate" "$CREDENTIALS_PATH"
      chmod 600 "$CREDENTIALS_PATH" || true
      return 0
    fi
  done

  die "Google OAuth Desktop client JSON is missing. Save it as ${CREDENTIALS_PATH}, or place it next to this script as credentials.json, then run again."
}

write_config() {
  info "Writing config: ${CONFIG_PATH}"
  mkdir -p "$CONFIG_DIR"

  cat > "$CONFIG_PATH" <<JSON
{
  "adc_credentials_path": "${ADC_PATH}",
  "allow_empty_source_delete": false,
  "auto_reauth_browser": true,
  "auto_reauth_min_interval_seconds": 21600,
  "auto_reauth_timeout_seconds": 300,
  "auto_reauth_timeout_retry_interval_seconds": 300,
  "bidirectional": true,
  "calendar_id": "primary",
  "conflict_policy": "newer_wins",
  "credentials_path": "${CREDENTIALS_PATH}",
  "default_duration_minutes": 30,
  "delete_stale": true,
  "google_popup_minutes": [],
  "include_lists": [],
  "list_policies": {},
  "lookahead_days": 365,
  "macos_notifications": true,
  "max_destructive_changes": 25,
  "max_destructive_ratio": 0.25,
  "destructive_approval_ttl_seconds": 600,
  "auto_approve_destructive_loops": 0,
  "notify_failure_min_interval_seconds": 300,
  "notify_success_min_interval_seconds": 3600,
  "prefix_list": false,
  "reminders_apply_path": "${APPLIER}",
  "reminders_exporter_path": "${EXPORTER}",
  "reminders_source": "eventkit",
  "reminders_sqlite_dir": "${HOME}/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores",
  "state_path": "${STATE_PATH}",
  "status_path": "${STATUS_PATH}",
  "sync_interval_seconds": 60,
  "target_service": "tasks",
  "tasks_complete_stale": true,
  "tasks_create_missing_lists": true,
  "tasks_import_unsynced": true,
  "tasks_mirror_empty_lists": true,
  "tasks_list_id": "",
  "tasks_list_title": "",
  "tasks_mirror_lists": true,
  "tasks_sync_undated": true,
  "token_path": "${TOKEN_PATH}",
  "transparency": "transparent",
  "use_adc": true,
  "verify_title_due_after_sync": true,
  "verify_title_due_retry_attempts": 3,
  "verify_title_due_retry_delay_seconds": 2.0
}
JSON

  chmod 600 "$CONFIG_PATH" || true
}

write_launch_agent() {
  info "Writing LaunchAgent: ${PLIST_PATH}"
  mkdir -p "${HOME}/Library/LaunchAgents"

  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>${SYNC_SCRIPT}</string>
    <string>--config</string>
    <string>${CONFIG_PATH}</string>
    <string>run-loop</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>WorkingDirectory</key>
  <string>${RUNTIME_CURRENT}</string>

  <key>ProcessType</key>
  <string>Background</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>30</integer>

  <key>StandardOutPath</key>
  <string>${STDOUT_LOG}</string>

  <key>StandardErrorPath</key>
  <string>${STDERR_LOG}</string>
</dict>
</plist>
PLIST

  plutil -lint "$PLIST_PATH" >/dev/null
}

run_google_setup() {
  if have gcloud; then
    if [ -n "$GCP_PROJECT" ]; then
      info "Setting Google Cloud project: ${GCP_PROJECT}"
      gcloud config set project "$GCP_PROJECT" >/dev/null || warn "Could not set gcloud project. Continuing."

      info "Ensuring Google Tasks API is enabled"
      gcloud services enable tasks.googleapis.com --project "$GCP_PROJECT" >/dev/null || warn "Could not enable Google Tasks API. If it is already enabled, this is usually fine."
    else
      warn "No Google Cloud project was supplied. Skipping project selection and Tasks API enablement."
      warn "Pass --project PROJECT_ID or set GCP_PROJECT when setup should manage those settings."
    fi
  else
    warn "gcloud is unavailable. Skipping Google Cloud project/API setup."
  fi

  if [ "$SKIP_GOOGLE_LOGIN" -eq 0 ]; then
    info "Starting Google ADC login"
    "$PYTHON_BIN" "$SYNC_SCRIPT" --config "$CONFIG_PATH" gcloud-login
  fi
}

check_reminders_access() {
  local lists_out
  local lists_err
  local export_status

  info "Checking Apple Reminders access"
  # The exporter output is TCC-protected Reminders content (list titles and the
  # account path). Fixed /tmp names would publish it to every local UID and let
  # a pre-planted symlink steer the redirect, so use unpredictable private
  # files and delete them on both paths.
  lists_out="$(umask 077 && mktemp "${TMPDIR:-/tmp}/icloud-reminders-google-sync-lists.XXXXXXXX")" \
    || die "Could not create a private temporary file for the Reminders list check."
  lists_err="$(umask 077 && mktemp "${TMPDIR:-/tmp}/icloud-reminders-google-sync-lists-err.XXXXXXXX")" \
    || die "Could not create a private temporary file for the Reminders list check."

  set +e
  swift "$EXPORTER" --lists-only >"$lists_out" 2>"$lists_err"
  export_status=$?
  set -e

  if [ "$export_status" -eq 0 ]; then
    printf 'Reminders lists detected:\n'
    sed -n '1,40p' "$lists_out"
    rm -f "$lists_out" "$lists_err"
  else
    warn "Could not read Reminders lists."
    warn "Open System Settings > Privacy & Security > Reminders and allow Terminal or your shell app, then run this script again."
    sed -n '1,80p' "$lists_err" >&2 || true
    rm -f "$lists_out" "$lists_err"
    exit 1
  fi
}

run_first_sync() {
  local dry_output
  local inserted

  if [ "$SKIP_FIRST_SYNC" -eq 1 ]; then
    return 0
  fi

  info "Running safe dry-run sync"
  set +e
  dry_output="$("$PYTHON_BIN" "$SYNC_SCRIPT" --config "$CONFIG_PATH" sync --dry-run --no-delete-stale 2>&1)"
  local dry_status=$?
  set -e
  printf '%s\n' "$dry_output"
  [ "$dry_status" -eq 0 ] || die "Dry-run sync failed. Fix the issue above, then run this script again."

  inserted="$(printf '%s\n' "$dry_output" | sed -n 's/.*inserted=\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
  inserted="${inserted:-0}"

  if [ "$inserted" -gt 20 ] && [ "$ASSUME_YES" -eq 0 ]; then
    warn "Dry-run says it would insert ${inserted} Google Tasks."
    confirm "Continue with the first real sync anyway?" || die "Stopped before real sync."
  fi

  info "Running first safe sync"
  "$PYTHON_BIN" "$SYNC_SCRIPT" --config "$CONFIG_PATH" sync --no-delete-stale
}

load_launch_agent() {
  info "Loading background LaunchAgent"
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
  sleep 2
  launchctl print "gui/$(id -u)/${LABEL}" | sed -n '1,80p'
}

info "Setting up iCloud Reminders -> Google Tasks sync"
info "Verified source directory: ${SCRIPT_DIR}"
info "Stable runtime directory: ${RUNTIME_CURRENT}"

[ -f "$SOURCE_SYNC_SCRIPT" ] || die "Missing sync script: ${SOURCE_SYNC_SCRIPT}"
[ -f "$SOURCE_EXPORTER" ] || die "Missing Reminders exporter: ${SOURCE_EXPORTER}"
[ -f "$SOURCE_APPLIER" ] || die "Missing Reminders applier: ${SOURCE_APPLIER}"
[ -f "$SOURCE_MANAGER" ] || die "Missing Google Tasks manager: ${SOURCE_MANAGER}"

if ! xcode-select -p >/dev/null 2>&1; then
  warn "Xcode Command Line Tools are missing."
  xcode-select --install || true
  die "Finish the Xcode Command Line Tools install, then run this script again."
fi

if ! find_python; then
  install_if_missing python3 "brew install python"
  find_python || die "Could not find Python after install."
fi

install_if_missing gcloud "brew install --cask gcloud-cli || brew install --cask google-cloud-sdk"
have swift || die "swift is missing. Install Xcode Command Line Tools, then run this script again."

install_runtime_release
mkdir -p "$CONFIG_DIR"
copy_credentials_if_needed
write_config
run_google_setup
check_reminders_access
run_first_sync
prepare_runtime_logs
write_launch_agent
install_management_launcher
load_launch_agent

info "Setup complete"
printf 'Config: %s\n' "$CONFIG_PATH"
printf 'Runtime release: %s\n' "$RUNTIME_RELEASE_DIR"
printf 'LaunchAgent: %s\n' "$PLIST_PATH"
printf 'Management launcher: %s\n' "$MANAGER_LAUNCHER"
printf 'Logs: %s %s\n' "$STDOUT_LOG" "$STDERR_LOG"
