#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


APP_NAME = "icloud-reminders-google-sync"
LAUNCH_AGENT_LABEL = "com.icloud-reminders-google-sync"
PROJECT_DIR = Path(__file__).resolve().parent
SYNC_MARKER_KEY = "irsync"
SYNC_MARKER_VALUE = "v1"
UID_KEY = "iruid"
DIGEST_KEY = "irdigest"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
LOCAL_OAUTH_SCOPES = " ".join([CALENDAR_SCOPE, TASKS_SCOPE, "openid", "email"])
APPLE_EPOCH_OFFSET_SECONDS = 978307200
GCLOUD_LOGIN_SCOPES = ",".join(
    [
        CALENDAR_SCOPE,
        TASKS_SCOPE,
        "https://www.googleapis.com/auth/cloud-platform",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    ]
)
LIST_POLICY_DIRECTIONS = {"bidirectional", "apple_to_google", "google_to_apple"}
LIST_POLICY_KEYS = {"direction", "delete_propagation", "sync_undated", "conflict_policy"}
CONFLICT_POLICIES = {"skip", "newer_wins"}
SAFE_OAUTH_ERROR_CODES = {
    "access_denied",
    "invalid_client",
    "invalid_grant",
    "invalid_request",
    "server_error",
    "temporarily_unavailable",
    "unauthorized_client",
    "unsupported_grant_type",
}
KNOWN_STATUS_STATES = {
    "account_binding_required",
    "auth_prompt_open",
    "auth_refreshed",
    "auth_required",
    "auth_timeout",
    "blocked_mutation_plan",
    "dry_run_ok",
    "failed",
    "ok",
    "running",
}


def default_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / APP_NAME


def default_config() -> dict[str, Any]:
    config_dir = default_config_dir()
    return {
        "use_adc": True,
        "adc_credentials_path": str(Path.home() / ".config/gcloud/application_default_credentials.json"),
        "credentials_path": str(config_dir / "credentials.json"),
        "token_path": str(config_dir / "token.json"),
        "state_path": str(config_dir / "state.json"),
        "status_path": str(config_dir / "status.json"),
        "auto_reauth_browser": False,
        "auto_reauth_min_interval_seconds": 21600,
        "auto_reauth_timeout_seconds": 300,
        "auto_reauth_timeout_retry_interval_seconds": 300,
        "macos_notifications": True,
        "notify_success_min_interval_seconds": 3600,
        "notify_failure_min_interval_seconds": 300,
        "verify_title_due_after_sync": True,
        "verify_title_due_retry_attempts": 3,
        "verify_title_due_retry_delay_seconds": 2.0,
        "max_destructive_changes": 25,
        "max_destructive_ratio": 0.25,
        "destructive_approval_ttl_seconds": 600,
        "auto_approve_destructive_loops": 0,
        "reminders_exporter_path": str(PROJECT_DIR / "RemindersExport.swift"),
        "reminders_apply_path": str(PROJECT_DIR / "RemindersApply.swift"),
        "reminders_source": "auto",
        "reminders_sqlite_dir": str(Path.home() / "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"),
        "target_service": "tasks",
        "calendar_id": "primary",
        "tasks_list_id": "",
        "tasks_list_title": "",
        "tasks_mirror_lists": True,
        "tasks_mirror_empty_lists": True,
        "tasks_create_missing_lists": True,
        "tasks_complete_stale": True,
        "tasks_import_unsynced": False,
        "tasks_sync_undated": False,
        "lookahead_days": 365,
        "sync_interval_seconds": 900,
        "default_duration_minutes": 30,
        "include_lists": [],
        "list_policies": {},
        "prefix_list": False,
        "delete_stale": True,
        "allow_empty_source_delete": False,
        "bidirectional": False,
        "conflict_policy": "newer_wins",
        "transparency": "transparent",
        "google_popup_minutes": [],
    }


class GoogleApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(google_api_failure_summary(status))


class OAuthTokenError(RuntimeError):
    def __init__(self, status: int, body: str, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body
        self.payload = payload or {}
        error = str(self.payload.get("error") or "")
        description = str(self.payload.get("error_description") or body)
        self.error = error
        self.error_description = description
        suffix = f", code={error}" if error in SAFE_OAUTH_ERROR_CODES else ""
        super().__init__(f"Google OAuth token request failed (HTTP {status}{suffix}).")

    @property
    def invalid_grant(self) -> bool:
        return self.error == "invalid_grant"


class AuthenticationRequired(RuntimeError):
    pass


class AccountBindingRequired(RuntimeError):
    pass


class OAuthCallbackTimeout(RuntimeError):
    pass


def google_api_failure_summary(status: int) -> str:
    if status in (401, 403):
        action = "Reauthorize Google access and confirm the Tasks API permission."
    elif status == 429:
        action = "Google rate-limited the request; wait before retrying."
    elif status >= 500:
        action = "Google is temporarily unavailable; retry later."
    else:
        action = "Review the private log, then retry the diagnostic."
    return f"Google API request failed (HTTP {status}). {action}"


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def harden_log_stream_mode(stream: Any) -> None:
    """Make a redirected log file private to this user.

    The LaunchAgent writes reminder and task titles to its stdout/stderr files.
    launchd recreates a missing StandardOutPath/StandardErrorPath itself, so an
    install-time chmod does not survive; re-apply 0600 to our own descriptor at
    startup. Working on the open descriptor rather than a path means a symlink
    or a replaced file cannot redirect the permission change.
    """

    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        info = os.fstat(fd)
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode):
        return
    if info.st_uid != os.getuid():
        return
    if not stat.S_IMODE(info.st_mode) & 0o077:
        return
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass


def harden_runtime_log_modes() -> None:
    for stream in (sys.stdout, sys.stderr):
        harden_log_stream_mode(stream)


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)


@contextlib.contextmanager
def sync_lock(config: dict[str, Any], wait: bool) -> Any:
    lock_path = expand_path(config["state_path"]).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            yield False
            return

        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str, length: int | None = None) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:length] if length else digest


class MutationPlanApprovalRequired(SystemExit):
    def __init__(self, message: str, summary: dict[str, Any]) -> None:
        self.summary = summary
        super().__init__(message)


def planned_mutation(
    target: str,
    operation: str,
    identity: Any,
    *,
    destructive: bool = False,
) -> dict[str, Any]:
    return {
        "target": target,
        "operation": operation,
        "resource": sha256_text(canonical_json(identity), 24),
        "destructive": bool(destructive),
    }


def build_mutation_plan(actions: list[dict[str, Any]], population: int) -> dict[str, Any]:
    normalized = sorted(
        (
            {
                "target": str(action["target"]),
                "operation": str(action["operation"]),
                "resource": str(action["resource"]),
                "destructive": bool(action.get("destructive")),
            }
            for action in actions
        ),
        key=lambda action: (
            action["target"],
            action["operation"],
            action["resource"],
            action["destructive"],
        ),
    )
    counts: dict[str, int] = {}
    destructive_counts: dict[str, int] = {}
    for action in normalized:
        key = f"{action['target']}.{action['operation']}"
        counts[key] = counts.get(key, 0) + 1
        if action["destructive"]:
            destructive_counts[key] = destructive_counts.get(key, 0) + 1

    destructive_count = sum(destructive_counts.values())
    safe_population = max(0, int(population))
    destructive_ratio = destructive_count / safe_population if safe_population else float(bool(destructive_count))
    fingerprint_material = {
        "version": 1,
        "population": safe_population,
        "actions": normalized,
    }
    return {
        "version": 1,
        "actions": normalized,
        "counts": dict(sorted(counts.items())),
        "destructive_counts": dict(sorted(destructive_counts.items())),
        "total_count": len(normalized),
        "destructive_count": destructive_count,
        "population": safe_population,
        "destructive_ratio": destructive_ratio,
        "fingerprint": sha256_text(canonical_json(fingerprint_material)),
    }


def mutation_plan_limit_reasons(config: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if int(plan["destructive_count"]) > int(config["max_destructive_changes"]):
        reasons.append("destructive_count_limit")
    if float(plan["destructive_ratio"]) > float(config["max_destructive_ratio"]):
        reasons.append("destructive_ratio_limit")
    return reasons


def mutation_plan_approval_token(
    plan: dict[str, Any],
    issued_at: dt.datetime | None = None,
) -> str:
    now = issued_at or dt.datetime.now(dt.timezone.utc)
    return f"{plan['fingerprint']}:{int(now.timestamp())}"


def next_blocked_plan_streak(
    previous_fingerprint: str,
    previous_streak: int,
    fingerprint: str,
) -> tuple[str, int]:
    """Track how many consecutive loop iterations produced the identical blocked plan."""
    if fingerprint and fingerprint == previous_fingerprint:
        return fingerprint, previous_streak + 1
    return fingerprint, 1 if fingerprint else 0


def should_auto_approve_blocked_plan(config: dict[str, Any], streak: int) -> bool:
    loops = int(config.get("auto_approve_destructive_loops", 0) or 0)
    return loops > 0 and streak >= loops


def validate_mutation_plan_approval(
    config: dict[str, Any],
    plan: dict[str, Any],
    approval: str,
    *,
    now: dt.datetime | None = None,
) -> tuple[bool, str]:
    if not approval:
        return False, "approval_missing"
    fingerprint, separator, raw_timestamp = approval.rpartition(":")
    if not separator or len(fingerprint) != 64 or not raw_timestamp.isdigit():
        return False, "approval_invalid"
    if not secrets.compare_digest(fingerprint, str(plan["fingerprint"])):
        return False, "approval_fingerprint_mismatch"

    checked_at = now or dt.datetime.now(dt.timezone.utc)
    issued_at = dt.datetime.fromtimestamp(int(raw_timestamp), tz=dt.timezone.utc)
    age_seconds = (checked_at - issued_at).total_seconds()
    if age_seconds < -60:
        return False, "approval_from_future"
    if age_seconds > int(config["destructive_approval_ttl_seconds"]):
        return False, "approval_expired"
    return True, "approved"


def sanitized_mutation_plan(plan: dict[str, Any], reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "version": int(plan["version"]),
        "fingerprint": str(plan["fingerprint"]),
        "total_count": int(plan["total_count"]),
        "destructive_count": int(plan["destructive_count"]),
        "population": int(plan["population"]),
        "destructive_ratio": round(float(plan["destructive_ratio"]), 6),
        "counts": dict(plan["counts"]),
        "destructive_counts": dict(plan["destructive_counts"]),
        "reasons": list(reasons or []),
    }


def enforce_mutation_plan(
    config: dict[str, Any],
    plan: dict[str, Any],
    *,
    dry_run: bool,
    now: dt.datetime | None = None,
) -> None:
    reasons = mutation_plan_limit_reasons(config, plan)
    ratio_percent = float(plan["destructive_ratio"]) * 100
    print(
        "Mutation plan: "
        f"total={plan['total_count']}, destructive={plan['destructive_count']}, "
        f"population={plan['population']}, destructive_ratio={ratio_percent:.2f}%"
    )
    print(f"Mutation plan fingerprint: {plan['fingerprint']}")
    if not reasons:
        return

    if dry_run:
        token = mutation_plan_approval_token(plan, issued_at=now)
        print("Destructive mutation plan exceeds the configured safety limit.")
        print(f"Short-lived approval token: {token}")
        return

    approved, approval_reason = validate_mutation_plan_approval(
        config,
        plan,
        str(config.get("_mutation_plan_approval") or ""),
        now=now,
    )
    if approved:
        print("Destructive mutation plan approval accepted.")
        return

    block_reasons = [*reasons, approval_reason]
    summary = sanitized_mutation_plan(plan, block_reasons)
    message = (
        "Mutation plan blocked before Apple/Google writes: "
        f"destructive={plan['destructive_count']}, population={plan['population']}, "
        f"ratio={ratio_percent:.2f}%, reason={approval_reason}. "
        "Run a dry-run and pass its matching short-lived token with --approve-mutation-plan."
    )
    write_sync_status(
        config,
        {
            "state": "blocked_mutation_plan",
            "last_end_at": utc_now_text(),
            "last_error": message,
            "mutation_plan": summary,
        },
    )
    raise MutationPlanApprovalRequired(message, summary)


def state_key(calendar_id: str, uid: str) -> str:
    return f"{sha256_text(calendar_id, 12)}:{uid}"


def target_state_key(target_id: str, uid: str) -> str:
    return f"{sha256_text(target_id, 12)}:{uid}"


def normalize_list_policies(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit("list_policies must be a JSON object keyed by Apple Reminders list title")

    normalized: dict[str, dict[str, Any]] = {}
    for raw_title, raw_policy in value.items():
        if not isinstance(raw_title, str) or not raw_title.strip():
            raise SystemExit("list_policies keys must be non-empty Apple Reminders list titles")
        if not isinstance(raw_policy, dict):
            raise SystemExit(f"list_policies[{raw_title!r}] must be a JSON object")

        unknown = sorted(set(raw_policy) - LIST_POLICY_KEYS)
        if unknown:
            raise SystemExit(
                f"Unsupported list_policies field(s) for {raw_title!r}: {', '.join(unknown)}. "
                f"Supported fields: {', '.join(sorted(LIST_POLICY_KEYS))}"
            )

        policy: dict[str, Any] = {}
        if "direction" in raw_policy:
            direction = raw_policy["direction"]
            if not isinstance(direction, str) or direction not in LIST_POLICY_DIRECTIONS:
                raise SystemExit(
                    f"Unsupported direction for list_policies[{raw_title!r}]. "
                    "Supported values: apple_to_google, google_to_apple, bidirectional"
                )
            policy["direction"] = direction

        for key in ("delete_propagation", "sync_undated"):
            if key not in raw_policy:
                continue
            if not isinstance(raw_policy[key], bool):
                raise SystemExit(f"list_policies[{raw_title!r}].{key} must be true or false")
            policy[key] = raw_policy[key]

        if "conflict_policy" in raw_policy:
            conflict_policy = raw_policy["conflict_policy"]
            if not isinstance(conflict_policy, str) or conflict_policy not in CONFLICT_POLICIES:
                raise SystemExit(
                    f"Unsupported conflict_policy for list_policies[{raw_title!r}]. "
                    "Supported values: skip, newer_wins"
                )
            policy["conflict_policy"] = conflict_policy

        normalized[raw_title] = policy
    return normalized


def list_sync_policy(config: dict[str, Any], list_title: str) -> dict[str, Any]:
    policies = config.get("list_policies") or {}
    override = policies.get(list_title, {}) if isinstance(policies, dict) else {}
    default_direction = "bidirectional" if config.get("bidirectional") else "apple_to_google"
    return {
        "direction": override.get("direction", default_direction),
        "delete_propagation": override.get("delete_propagation", bool(config.get("delete_stale"))),
        "sync_undated": override.get("sync_undated", bool(config.get("tasks_sync_undated"))),
        "conflict_policy": override.get("conflict_policy", str(config.get("conflict_policy") or "skip")),
    }


def list_allows_apple_to_google(config: dict[str, Any], list_title: str) -> bool:
    return list_sync_policy(config, list_title)["direction"] in ("apple_to_google", "bidirectional")


def list_allows_google_to_apple(config: dict[str, Any], list_title: str) -> bool:
    return list_sync_policy(config, list_title)["direction"] in ("google_to_apple", "bidirectional")


def list_allows_delete_propagation(
    config: dict[str, Any],
    list_title: str,
    *,
    safety_allows_deletes: bool = True,
) -> bool:
    if config.get("_disable_delete_propagation"):
        return False
    return bool(safety_allows_deletes and list_sync_policy(config, list_title)["delete_propagation"])


def reminders_export_needs_undated(config: dict[str, Any]) -> bool:
    if config.get("tasks_sync_undated"):
        return True
    if str(config.get("target_service") or "tasks") != "tasks":
        return False
    policies = config.get("list_policies") or {}
    return bool(
        isinstance(policies, dict)
        and any(isinstance(policy, dict) and policy.get("sync_undated") is True for policy in policies.values())
    )


def reminder_is_undated(reminder: dict[str, Any]) -> bool:
    return task_due_date(reminder) is None


def load_config(args: argparse.Namespace) -> dict[str, Any]:
    config = default_config()
    config_path = expand_path(args.config)

    if config_path.exists():
        loaded = read_json(config_path)
        if not isinstance(loaded, dict):
            raise SystemExit(f"Config file is not a JSON object: {config_path}")
        config.update(loaded)

    for key in (
        "credentials_path",
        "adc_credentials_path",
        "token_path",
        "state_path",
        "status_path",
        "auto_reauth_browser",
        "auto_reauth_min_interval_seconds",
        "auto_reauth_timeout_seconds",
        "auto_reauth_timeout_retry_interval_seconds",
        "macos_notifications",
        "notify_success_min_interval_seconds",
        "notify_failure_min_interval_seconds",
        "verify_title_due_after_sync",
        "verify_title_due_retry_attempts",
        "verify_title_due_retry_delay_seconds",
        "max_destructive_changes",
        "max_destructive_ratio",
        "destructive_approval_ttl_seconds",
        "reminders_exporter_path",
        "reminders_apply_path",
        "reminders_source",
        "reminders_sqlite_dir",
        "target_service",
        "calendar_id",
        "tasks_list_id",
        "tasks_list_title",
        "tasks_mirror_lists",
        "tasks_mirror_empty_lists",
        "tasks_create_missing_lists",
        "tasks_complete_stale",
        "tasks_import_unsynced",
        "tasks_sync_undated",
        "lookahead_days",
        "sync_interval_seconds",
        "default_duration_minutes",
        "include_lists",
        "list_policies",
        "prefix_list",
        "delete_stale",
        "allow_empty_source_delete",
        "bidirectional",
        "conflict_policy",
        "transparency",
        "google_popup_minutes",
        "use_adc",
    ):
        if hasattr(args, key) and getattr(args, key) is not None:
            config[key] = getattr(args, key)

    if getattr(args, "no_adc", False):
        config["use_adc"] = False
    if getattr(args, "no_delete_stale", False):
        config["delete_stale"] = False
    config["_disable_delete_propagation"] = bool(getattr(args, "no_delete_stale", False))
    if getattr(args, "no_tasks_mirror_lists", False):
        config["tasks_mirror_lists"] = False
    if getattr(args, "no_tasks_mirror_empty_lists", False):
        config["tasks_mirror_empty_lists"] = False
    if getattr(args, "no_tasks_create_missing_lists", False):
        config["tasks_create_missing_lists"] = False
    if getattr(args, "no_tasks_complete_stale", False):
        config["tasks_complete_stale"] = False
    if getattr(args, "tasks_import_unsynced", False):
        config["tasks_import_unsynced"] = True
    if getattr(args, "no_tasks_import_unsynced", False):
        config["tasks_import_unsynced"] = False
    if getattr(args, "tasks_sync_undated", False):
        config["tasks_sync_undated"] = True
    if getattr(args, "no_tasks_sync_undated", False):
        config["tasks_sync_undated"] = False
    if getattr(args, "no_macos_notifications", False):
        config["macos_notifications"] = False
    if getattr(args, "no_verify_title_due_after_sync", False):
        config["verify_title_due_after_sync"] = False
    if getattr(args, "busy", False):
        config["transparency"] = "opaque"

    for path_key in (
        "credentials_path",
        "adc_credentials_path",
        "token_path",
        "state_path",
        "status_path",
        "reminders_exporter_path",
        "reminders_apply_path",
        "reminders_sqlite_dir",
    ):
        config[path_key] = str(expand_path(config[path_key]))

    config["_config_path"] = str(config_path)
    config["auto_reauth_browser"] = bool(config.get("auto_reauth_browser"))
    config["auto_reauth_min_interval_seconds"] = int(config.get("auto_reauth_min_interval_seconds") or 21600)
    config["auto_reauth_timeout_seconds"] = int(config.get("auto_reauth_timeout_seconds") or 300)
    config["auto_reauth_timeout_retry_interval_seconds"] = int(
        config.get("auto_reauth_timeout_retry_interval_seconds") or 300
    )
    config["macos_notifications"] = bool(config.get("macos_notifications"))
    config["notify_success_min_interval_seconds"] = int(config.get("notify_success_min_interval_seconds") or 3600)
    config["notify_failure_min_interval_seconds"] = int(config.get("notify_failure_min_interval_seconds") or 300)
    config["verify_title_due_after_sync"] = bool(config.get("verify_title_due_after_sync"))
    config["verify_title_due_retry_attempts"] = max(1, int(config.get("verify_title_due_retry_attempts") or 1))
    config["verify_title_due_retry_delay_seconds"] = max(
        0.0,
        float(config.get("verify_title_due_retry_delay_seconds") or 0.0),
    )
    config["max_destructive_changes"] = max(0, int(config.get("max_destructive_changes") or 0))
    config["max_destructive_ratio"] = min(
        1.0,
        max(0.0, float(config.get("max_destructive_ratio") or 0.0)),
    )
    config["destructive_approval_ttl_seconds"] = max(
        60,
        int(config.get("destructive_approval_ttl_seconds") or 600),
    )
    config["_mutation_plan_approval"] = str(getattr(args, "approve_mutation_plan", "") or "").strip()
    config["lookahead_days"] = int(config["lookahead_days"])
    config["sync_interval_seconds"] = int(config["sync_interval_seconds"])
    config["default_duration_minutes"] = int(config["default_duration_minutes"])
    config["include_lists"] = list(config.get("include_lists") or [])
    config["list_policies"] = normalize_list_policies(config.get("list_policies"))
    config["google_popup_minutes"] = [int(value) for value in config.get("google_popup_minutes") or []]
    config["prefix_list"] = bool(config.get("prefix_list"))
    config["delete_stale"] = bool(config.get("delete_stale"))
    config["allow_empty_source_delete"] = bool(config.get("allow_empty_source_delete"))
    config["bidirectional"] = bool(config.get("bidirectional"))
    config["tasks_mirror_lists"] = bool(config.get("tasks_mirror_lists"))
    config["tasks_mirror_empty_lists"] = bool(config.get("tasks_mirror_empty_lists"))
    config["tasks_create_missing_lists"] = bool(config.get("tasks_create_missing_lists"))
    config["tasks_complete_stale"] = bool(config.get("tasks_complete_stale"))
    config["tasks_import_unsynced"] = bool(config.get("tasks_import_unsynced"))
    config["tasks_sync_undated"] = bool(config.get("tasks_sync_undated"))
    config["use_adc"] = bool(config.get("use_adc"))
    config["reminders_source"] = str(config.get("reminders_source") or "auto")
    config["target_service"] = str(config.get("target_service") or "tasks")
    config["tasks_list_id"] = str(config.get("tasks_list_id") or "")
    config["tasks_list_title"] = str(config.get("tasks_list_title") or "")
    config["conflict_policy"] = str(config.get("conflict_policy") or "skip")
    config["transparency"] = str(config.get("transparency") or "transparent")
    if config["target_service"] not in ("tasks", "calendar"):
        raise SystemExit("Unsupported target_service. Supported values: tasks, calendar")
    if config["conflict_policy"] not in CONFLICT_POLICIES:
        raise SystemExit("Unsupported conflict_policy. Currently supported: skip, newer_wins")
    return config


def load_credentials(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(
            f"Google OAuth credentials not found: {path}\n"
            "Create a Desktop OAuth client in Google Cloud, enable the Google Calendar/Tasks APIs, "
            "download the JSON, and place it at this path."
        )

    raw = read_json(path)
    client = raw.get("installed") or raw.get("web") or raw
    client_id = client.get("client_id")
    client_secret = client.get("client_secret", "")
    if not client_id:
        raise SystemExit(f"Could not find client_id in {path}")
    return {"client_id": client_id, "client_secret": client_secret}


def load_adc_credentials(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None

    raw = read_json(path)
    if raw.get("type") != "authorized_user":
        raise SystemExit(f"Unsupported ADC credentials type in {path}: {raw.get('type')}")
    if not raw.get("refresh_token") or not raw.get("client_id"):
        raise SystemExit(
            f"ADC credentials are missing refresh_token/client_id: {path}\n"
            "Run: gcloud auth application-default login "
            f"--scopes={GCLOUD_LOGIN_SCOPES}"
        )

    raw["_credential_source"] = "gcloud_adc"
    return raw


def make_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")


def make_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.oauth_result = {key: values[0] for key, values in params.items()}  # type: ignore[attr-defined]

        body = (
            "<html><body><h1>Authorization received</h1>"
            "<p>You can close this browser tab and return to the terminal.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: Any) -> None:
        return


def is_transient_url_error(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", exc)
    text = str(reason).lower()
    return isinstance(reason, TimeoutError) or "timed out" in text or "timeout" in text


def post_form(url: str, fields: dict[str, str], attempts: int = 3) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    last_error: urllib.error.URLError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            payload = None
            try:
                decoded = json.loads(body)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                payload = None
            raise OAuthTokenError(exc.code, body, payload) from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < attempts and is_transient_url_error(exc):
                time.sleep(min(2**attempt, 10))
                continue
            raise OAuthTokenError(0, f"OAuth request failed: {exc.reason}") from exc

    raise OAuthTokenError(0, f"OAuth request failed: {last_error}")


def open_auth_url(url: str) -> bool:
    if sys.platform == "darwin" and shutil.which("open"):
        result = subprocess.run(["open", url], check=False)
        if result.returncode == 0:
            return True

    return webbrowser.open(url)


def run_auth_flow(config: dict[str, Any]) -> None:
    credentials = load_credentials(expand_path(config["credentials_path"]))
    token_path = expand_path(config["token_path"])
    verifier = make_code_verifier()
    state = secrets.token_urlsafe(24)

    server = http.server.HTTPServer(("127.0.0.1", 0), OAuthCallbackHandler)
    server.oauth_result = None  # type: ignore[attr-defined]
    server.timeout = 1
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"

    query = {
        "client_id": credentials["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": LOCAL_OAUTH_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": make_code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    auth_url = f"{OAUTH_AUTH_URL}?{urllib.parse.urlencode(query)}"

    print("Opening Google OAuth consent in your browser.")
    if not open_auth_url(auth_url):
        print("Open this URL manually:")
        print(auth_url)

    timeout_seconds = max(60, int(config.get("auto_reauth_timeout_seconds") or 300))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and server.oauth_result is None:  # type: ignore[attr-defined]
        server.handle_request()

    result = server.oauth_result  # type: ignore[attr-defined]
    server.server_close()

    if not result:
        raise OAuthCallbackTimeout("Timed out waiting for Google OAuth callback.")
    if result.get("state") != state:
        raise SystemExit("OAuth state mismatch; refusing to save token.")
    if result.get("error"):
        raise SystemExit(f"OAuth error: {result['error']}")
    if not result.get("code"):
        raise SystemExit("OAuth callback did not include an authorization code.")

    fields = {
        "code": result["code"],
        "client_id": credentials["client_id"],
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if credentials.get("client_secret"):
        fields["client_secret"] = credentials["client_secret"]

    try:
        token = post_form(OAUTH_TOKEN_URL, fields)
    except OAuthTokenError as exc:
        raise SystemExit(str(exc)) from exc
    token["created_at"] = int(time.time())
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600)) - 60
    token["_credential_source"] = "local_oauth"
    write_json_atomic(token_path, token)
    print(f"Saved Google OAuth token: {token_path}")


def same_refresh_token(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(left.get("refresh_token")) and (
        str(left.get("refresh_token")) == str(right.get("refresh_token"))
        and str(left.get("client_id") or "") == str(right.get("client_id") or "")
    )


def load_token_candidates(config: dict[str, Any]) -> list[dict[str, Any]]:
    token_path = expand_path(config["token_path"])
    candidates: list[dict[str, Any]] = []

    if config.get("use_adc"):
        adc_token = load_adc_credentials(expand_path(config["adc_credentials_path"]))
        if adc_token:
            candidates.append(adc_token)

    if token_path.exists():
        local_token = read_json(token_path)
        if "_credential_source" not in local_token:
            local_token["_credential_source"] = "local_oauth"
        if not any(same_refresh_token(local_token, candidate) for candidate in candidates):
            candidates.append(local_token)

    return candidates


def load_token(config: dict[str, Any]) -> dict[str, Any]:
    candidates = load_token_candidates(config)
    if candidates:
        return candidates[0]

    token_path = expand_path(config["token_path"])
    raise SystemExit(
        "Google OAuth token not found.\n"
        f"Local token: {token_path}\n"
        f"gcloud ADC: {expand_path(config['adc_credentials_path'])}\n"
        "Run either:\n"
        f"  gcloud auth application-default login --scopes={GCLOUD_LOGIN_SCOPES}\n"
        "or:\n"
        "  python3 icloud_reminders_google_sync.py auth"
    )


def load_fallback_token(config: dict[str, Any], failed_token: dict[str, Any]) -> dict[str, Any] | None:
    for candidate in load_token_candidates(config):
        if same_refresh_token(candidate, failed_token):
            continue
        return candidate
    return None


def google_reauth_message(config: dict[str, Any]) -> str:
    config_path = str(config.get("_config_path") or (default_config_dir() / "config.json"))
    python_bin = sys.executable or "python3"
    return (
        "Google OAuth refresh token has expired or was revoked. "
        "This cannot be repaired fully in the background.\n"
        "Run this once from Terminal, complete the browser login, then the LaunchAgent will resume on its next cycle:\n"
        f"  cd {PROJECT_DIR}\n"
        f"  {python_bin} {Path(__file__).name} --config {config_path} gcloud-login\n"
        "If gcloud is unavailable, use:\n"
        f"  {python_bin} {Path(__file__).name} --config {config_path} auth"
    )


def refresh_token(config: dict[str, Any], token: dict[str, Any]) -> dict[str, Any]:
    refresh = token.get("refresh_token")
    if not refresh:
        raise SystemExit("Token has no refresh_token. Run auth again with prompt=consent.")

    if token.get("client_id"):
        credentials = {
            "client_id": str(token["client_id"]),
            "client_secret": str(token.get("client_secret") or ""),
        }
    else:
        credentials = load_credentials(expand_path(config["credentials_path"]))

    fields = {
        "client_id": credentials["client_id"],
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }
    if credentials.get("client_secret"):
        fields["client_secret"] = credentials["client_secret"]

    new_token = post_form(OAUTH_TOKEN_URL, fields)
    merged = dict(token)
    merged.update(new_token)
    merged["refresh_token"] = new_token.get("refresh_token", refresh)
    if credentials.get("client_id"):
        merged["client_id"] = credentials["client_id"]
    if credentials.get("client_secret"):
        merged["client_secret"] = credentials["client_secret"]
    merged["_credential_source"] = str(token.get("_credential_source") or "local_oauth")
    merged["created_at"] = int(time.time())
    merged["expires_at"] = int(time.time()) + int(merged.get("expires_in", 3600)) - 60
    if merged.get("_credential_source") != "gcloud_adc":
        write_json_atomic(expand_path(config["token_path"]), merged)
    return merged


def refresh_token_with_fallback(config: dict[str, Any], token: dict[str, Any]) -> dict[str, Any]:
    require_expected_google_credential_binding(config, token)
    try:
        return refresh_token(config, token)
    except OAuthTokenError as exc:
        if not exc.invalid_grant:
            raise SystemExit(str(exc)) from exc

        fallback = load_fallback_token(config, token)
        if fallback:
            # load_fallback_token only ever returns a candidate whose refresh
            # token differs from the one that just failed, while the binding is
            # derived from that same failed token. A fallback that belongs to a
            # different credential is therefore the normal case, not an
            # emergency: it is unusable, so drop it and ask for reauthorization.
            # Raising here instead left the loop with no way forward, because
            # the actionable AuthenticationRequired below was never reached.
            try:
                require_expected_google_credential_binding(config, fallback)
            except AccountBindingRequired:
                fallback = None

        if fallback:
            try:
                return refresh_token(config, fallback)
            except OAuthTokenError as fallback_exc:
                if not fallback_exc.invalid_grant:
                    raise SystemExit(str(fallback_exc)) from fallback_exc

        raise AuthenticationRequired(google_reauth_message(config)) from exc


class GoogleCalendarClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.token = load_token(config)

    def access_token(self) -> str:
        require_expected_google_credential_binding(self.config, self.token)
        if int(self.token.get("expires_at", 0)) <= int(time.time()) + 60:
            self.token = refresh_token_with_fallback(self.config, self.token)
        return str(self.token["access_token"])

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
        url = f"{CALENDAR_API}{path}{query}"
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token()}",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and retry:
                self.token = refresh_token_with_fallback(self.config, self.token)
                return self.request(method, path, params=params, body=body, retry=False)
            raise GoogleApiError(exc.code, f"{method} {path}: {raw}") from exc

    def list_synced_events(self, calendar_id: str) -> list[dict[str, Any]]:
        encoded_calendar = urllib.parse.quote(calendar_id, safe="")
        params: dict[str, Any] = {
            "privateExtendedProperty": f"{SYNC_MARKER_KEY}={SYNC_MARKER_VALUE}",
            "showDeleted": "false",
            "singleEvents": "true",
            "maxResults": 2500,
        }
        events: list[dict[str, Any]] = []

        while True:
            response = self.request("GET", f"/calendars/{encoded_calendar}/events", params=params)
            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token

        return events

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        encoded_calendar = urllib.parse.quote(calendar_id, safe="")
        return self.request("POST", f"/calendars/{encoded_calendar}/events", body=body)

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        encoded_calendar = urllib.parse.quote(calendar_id, safe="")
        encoded_event = urllib.parse.quote(event_id, safe="")
        return self.request("PUT", f"/calendars/{encoded_calendar}/events/{encoded_event}", body=body)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        encoded_calendar = urllib.parse.quote(calendar_id, safe="")
        encoded_event = urllib.parse.quote(event_id, safe="")
        try:
            self.request("DELETE", f"/calendars/{encoded_calendar}/events/{encoded_event}")
        except GoogleApiError as exc:
            if exc.status not in (404, 410):
                raise


class GoogleTasksClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.token = load_token(config)

    def access_token(self) -> str:
        require_expected_google_credential_binding(self.config, self.token)
        if int(self.token.get("expires_at", 0)) <= int(time.time()) + 60:
            self.token = refresh_token_with_fallback(self.config, self.token)
        return str(self.token["access_token"])

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
        url = f"{TASKS_API}{path}{query}"
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token()}",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and retry:
                self.token = refresh_token_with_fallback(self.config, self.token)
                return self.request(method, path, params=params, body=body, retry=False)
            raise GoogleApiError(exc.code, raw) from exc

    def list_tasklists(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"maxResults": 1000}
        lists: list[dict[str, Any]] = []

        while True:
            response = self.request("GET", "/users/@me/lists", params=params)
            lists.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token

        return lists

    def insert_tasklist(self, title: str) -> dict[str, Any]:
        return self.request("POST", "/users/@me/lists", body={"title": title})

    def resolve_tasklist_id(self) -> str:
        configured_id = str(self.config.get("tasks_list_id") or "").strip()
        if configured_id:
            return configured_id

        tasklists = self.list_tasklists()
        if not tasklists:
            raise SystemExit("No Google Tasks task lists were found.")

        title = str(self.config.get("tasks_list_title") or "").strip()
        if title:
            for tasklist in tasklists:
                if str(tasklist.get("title") or "") == title:
                    return str(tasklist["id"])
            raise SystemExit(f"Google Tasks list not found by title: {title}")

        return str(tasklists[0]["id"])

    def list_tasks(self, tasklist_id: str, show_deleted: bool = False) -> list[dict[str, Any]]:
        encoded_list = urllib.parse.quote(tasklist_id, safe="")
        params: dict[str, Any] = {
            "showCompleted": "true",
            "showDeleted": "true" if show_deleted else "false",
            "showHidden": "true",
            "maxResults": 100,
        }
        tasks: list[dict[str, Any]] = []

        while True:
            response = self.request("GET", f"/lists/{encoded_list}/tasks", params=params)
            tasks.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token

        return tasks

    def get_task(self, tasklist_id: str, task_id: str) -> dict[str, Any]:
        encoded_list = urllib.parse.quote(tasklist_id, safe="")
        encoded_task = urllib.parse.quote(task_id, safe="")
        return self.request("GET", f"/lists/{encoded_list}/tasks/{encoded_task}")

    def insert_task(self, tasklist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        encoded_list = urllib.parse.quote(tasklist_id, safe="")
        return self.request("POST", f"/lists/{encoded_list}/tasks", body=body)

    def patch_task(self, tasklist_id: str, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        encoded_list = urllib.parse.quote(tasklist_id, safe="")
        encoded_task = urllib.parse.quote(task_id, safe="")
        return self.request("PATCH", f"/lists/{encoded_list}/tasks/{encoded_task}", body=body)

    def delete_task(self, tasklist_id: str, task_id: str) -> None:
        encoded_list = urllib.parse.quote(tasklist_id, safe="")
        encoded_task = urllib.parse.quote(task_id, safe="")
        try:
            self.request("DELETE", f"/lists/{encoded_list}/tasks/{encoded_task}")
        except GoogleApiError as exc:
            if exc.status not in (404, 410):
                raise


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "events": {}, "tasks": {}}
    state = read_json(path)
    if not isinstance(state.get("events"), dict):
        state["events"] = {}
    if not isinstance(state.get("tasks"), dict):
        state["tasks"] = {}
    return state


def google_credential_binding(token: dict[str, Any]) -> str:
    refresh = str((token or {}).get("refresh_token") or "")
    if not refresh:
        raise AuthenticationRequired(
            "Google OAuth credential identity is unavailable. Reauthorize Google access before syncing."
        )
    return sha256_text(f"google-oauth-refresh\n{refresh}")


def require_expected_google_credential_binding(config: dict[str, Any], token: dict[str, Any]) -> None:
    expected = str(config.get("_expected_google_credential_binding") or "")
    if expected and google_credential_binding(token) != expected:
        raise AccountBindingRequired(
            "Google OAuth credentials changed during fallback; refusing to reuse the existing sync state."
        )


def apple_account_binding(records: list[dict[str, Any]]) -> str:
    account_ids = sorted({
        str(record.get("account_id") or "").strip()
        for record in records
        if isinstance(record, dict) and str(record.get("account_id") or "").strip()
    })
    if not account_ids:
        raise AccountBindingRequired(
            "Apple Reminders account identity is unavailable; refusing to reuse sync state."
        )
    return sha256_text(canonical_json({"apple_account_ids": account_ids}))


def resolve_sync_account_binding(
    config: dict[str, Any],
    reminders: list[dict[str, Any]],
) -> dict[str, Any]:
    # Lists cover empty accounts and lists whose reminders fall outside the
    # current lookahead window. Only opaque hashes are persisted.
    reminder_lists = run_reminders_lists_export(config)
    apple_records = [*reminder_lists, *reminders]
    token = load_token(config)
    return {
        "version": 1,
        "apple": apple_account_binding(apple_records),
        "google": google_credential_binding(token),
    }


def bind_or_validate_sync_state_accounts(
    state: dict[str, Any],
    binding: dict[str, Any],
) -> None:
    normalized = {
        "version": 1,
        "apple": str(binding.get("apple") or ""),
        "google": str(binding.get("google") or ""),
    }
    if not normalized["apple"] or not normalized["google"]:
        raise AccountBindingRequired("Sync account binding is incomplete; refusing to continue.")

    existing = state.get("account_binding")
    has_legacy_records = bool(state.get("events") or state.get("tasks"))
    if existing is None:
        if has_legacy_records:
            raise AccountBindingRequired(
                "Existing sync state has no account binding; back it up and explicitly rebuild state before syncing."
            )
        state["account_binding"] = normalized
        return
    if not isinstance(existing, dict):
        raise AccountBindingRequired("Stored sync account binding is invalid; refusing to continue.")
    current = {
        "version": int(existing.get("version") or 0),
        "apple": str(existing.get("apple") or ""),
        "google": str(existing.get("google") or ""),
    }
    if current != normalized:
        raise AccountBindingRequired(
            "Apple Reminders or Google OAuth account binding changed; refusing to reuse the existing sync state."
        )


def run_reminders_export(config: dict[str, Any], completed_only: bool = False) -> list[dict[str, Any]]:
    source = str(config.get("reminders_source") or "auto")
    if source == "sqlite":
        return run_reminders_sqlite_export(config, completed_only=completed_only)
    if source not in ("auto", "eventkit"):
        raise SystemExit(f"Unknown reminders_source: {source}")

    try:
        return run_reminders_eventkit_export(config, completed_only=completed_only)
    except SystemExit as exc:
        if source == "eventkit":
            raise
        eprint(f"EventKit Reminders export failed; falling back to SQLite export. {exc}")
        return run_reminders_sqlite_export(config, completed_only=completed_only)


def run_reminders_eventkit_export(config: dict[str, Any], completed_only: bool = False) -> list[dict[str, Any]]:
    exporter = expand_path(config["reminders_exporter_path"])
    if not exporter.exists():
        raise SystemExit(f"Reminders exporter not found: {exporter}")
    if not shutil.which("swift"):
        raise SystemExit("Swift is not installed or not on PATH. This sync requires macOS Swift/EventKit.")

    command = [
        "swift",
        str(exporter),
        "--lookahead-days",
        str(config["lookahead_days"]),
    ]
    if reminders_export_needs_undated(config):
        command.append("--include-undated")
    if completed_only:
        command.append("--completed-only")
    for list_name in config["include_lists"]:
        command.extend(["--list", list_name])

    process = subprocess.run(command, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        hint = (
            "\nIf this is a macOS privacy error, run the command once from Terminal and allow "
            "Reminders access in System Settings > Privacy & Security > Reminders."
        )
        raise SystemExit(
            "Failed to read Apple Reminders.\n"
            f"Command: {' '.join(command)}\n"
            f"{process.stderr.strip()}"
            f"{hint}"
        )

    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Reminders exporter returned invalid JSON: {exc}\n{process.stdout}") from exc

    if not isinstance(payload, list):
        raise SystemExit("Reminders exporter did not return a JSON array.")
    return payload


def run_reminders_lists_export(config: dict[str, Any]) -> list[dict[str, Any]]:
    source = str(config.get("reminders_source") or "auto")
    if source == "sqlite":
        return run_reminders_sqlite_lists_export(config)
    if source not in ("auto", "eventkit"):
        raise SystemExit(f"Unknown reminders_source: {source}")

    try:
        return run_reminders_eventkit_lists_export(config)
    except SystemExit as exc:
        if source == "eventkit":
            raise
        eprint(f"EventKit Reminders lists export failed; falling back to SQLite. {exc}")
        return run_reminders_sqlite_lists_export(config)


def run_reminders_eventkit_lists_export(config: dict[str, Any]) -> list[dict[str, Any]]:
    exporter = expand_path(config["reminders_exporter_path"])
    if not exporter.exists():
        raise SystemExit(f"Reminders exporter not found: {exporter}")
    if not shutil.which("swift"):
        raise SystemExit("Swift is not installed or not on PATH. This sync requires macOS Swift/EventKit.")

    command = [
        "swift",
        str(exporter),
        "--lists-only",
    ]
    for list_name in config["include_lists"]:
        command.extend(["--list", list_name])

    process = subprocess.run(command, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        raise SystemExit(
            "Failed to read Apple Reminders lists.\n"
            f"Command: {' '.join(command)}\n"
            f"{process.stderr.strip()}"
        )

    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Reminders lists exporter returned invalid JSON: {exc}\n{process.stdout}") from exc

    if not isinstance(payload, list):
        raise SystemExit("Reminders lists exporter did not return a JSON array.")
    return payload


def apple_timestamp_to_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    try:
        seconds = float(value) + APPLE_EPOCH_OFFSET_SECONDS
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)


def apple_timestamp_to_iso(value: Any) -> str | None:
    date = apple_timestamp_to_datetime(value)
    return format_rfc3339(date) if date else None


def apple_timestamp_to_local_date(value: Any) -> str | None:
    date = apple_timestamp_to_datetime(value)
    if not date:
        return None
    return date.astimezone().date().isoformat()


def blob_to_hex(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def list_reminders_sqlite_paths(config: dict[str, Any]) -> list[Path]:
    stores_dir = expand_path(config["reminders_sqlite_dir"])
    if not stores_dir.exists():
        raise SystemExit(f"Reminders SQLite store directory not found: {stores_dir}")
    return sorted(
        path
        for path in stores_dir.glob("Data-*.sqlite")
        if path.name != "Data-local.sqlite" and not path.name.endswith(("-wal", "-shm"))
    )


def run_reminders_sqlite_export(config: dict[str, Any], completed_only: bool = False) -> list[dict[str, Any]]:
    latest_date = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=int(config["lookahead_days"]))
    list_filter = set(config["include_lists"])
    include_undated = reminders_export_needs_undated(config)
    reminders: dict[str, dict[str, Any]] = {}
    paths = list_reminders_sqlite_paths(config)

    query = """
        SELECT
            r.Z_PK AS pk,
            r.ZTITLE AS title,
            r.ZNOTES AS notes,
            r.ZCOMPLETED AS completed,
            r.ZMARKEDFORDELETION AS marked_for_deletion,
            r.ZPRIORITY AS priority,
            r.ZALLDAY AS all_day,
            r.ZDISPLAYDATEISALLDAY AS display_date_is_all_day,
            r.ZCREATIONDATE AS creation_date,
            r.ZLASTMODIFIEDDATE AS modified_date,
            r.ZCOMPLETIONDATE AS completion_date,
            r.ZDISPLAYDATEDATE AS display_date,
            r.ZDUEDATE AS due_date,
            r.ZSTARTDATE AS start_date,
            r.ZTIMEZONE AS timezone,
            r.ZDISPLAYDATETIMEZONE AS display_timezone,
            r.ZEXTERNALIDENTIFIER AS external_id,
            r.ZIDENTIFIER AS identifier,
            r.ZLIST AS list_pk,
            l.ZNAME AS list_title,
            l.ZEXTERNALIDENTIFIER AS list_external_id,
            l.ZIDENTIFIER AS list_identifier
        FROM ZREMCDREMINDER r
        LEFT JOIN ZREMCDBASELIST l ON l.Z_PK = r.ZLIST
        WHERE COALESCE(r.ZMARKEDFORDELETION, 0) = 0
    """
    if completed_only:
        query += """
          AND COALESCE(r.ZCOMPLETED, 0) != 0
        """
    else:
        query += """
          AND COALESCE(r.ZCOMPLETED, 0) = 0
        """
    if not completed_only and not include_undated:
        query += """
          AND (r.ZDUEDATE IS NOT NULL OR r.ZDISPLAYDATEDATE IS NOT NULL OR r.ZSTARTDATE IS NOT NULL)
        """

    for path in paths:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            eprint(f"Skipping unreadable Reminders store {path}: {exc}")
            continue

        with connection:
            try:
                rows = connection.execute(query).fetchall()
            except sqlite3.Error as exc:
                eprint(f"Skipping incompatible Reminders store {path}: {exc}")
                continue

        account_id = path.stem.replace("Data-", "")
        account_title = account_id
        for row in rows:
            list_title = str(row["list_title"] or "")
            if list_filter and list_title not in list_filter:
                continue

            raw_date = row["due_date"] if row["due_date"] is not None else row["display_date"]
            if raw_date is None:
                raw_date = row["start_date"]
            chosen_date = apple_timestamp_to_datetime(raw_date)
            if not completed_only and not chosen_date and not include_undated:
                continue
            if not completed_only and chosen_date and chosen_date > latest_date:
                continue

            all_day = bool(row["all_day"] or row["display_date_is_all_day"])
            external_id = str(row["external_id"] or "")
            identifier = blob_to_hex(row["identifier"])
            stable_id = external_id or identifier or f"{account_id}:{row['pk']}"
            list_id = str(row["list_external_id"] or blob_to_hex(row["list_identifier"]) or row["list_pk"] or "")
            uid_seed = f"{account_id}:{stable_id}"

            item = {
                "id": f"{account_id}:{row['pk']}",
                "external_id": external_id,
                "stable_id": stable_id,
                "title": row["title"] or "",
                "notes": row["notes"] or "",
                "list_title": list_title,
                "list_id": list_id,
                "account_title": account_title,
                "account_id": account_id,
                "priority": row["priority"] or 0,
                "is_completed": bool(row["completed"]),
                "is_recurring": False,
                "recurrence_count": 0,
                "created_at": apple_timestamp_to_iso(row["creation_date"]),
                "modified_at": apple_timestamp_to_iso(row["modified_date"]),
                "completed_at": apple_timestamp_to_iso(row["completion_date"]),
                "due_at": apple_timestamp_to_iso(raw_date),
                "due_date": apple_timestamp_to_local_date(raw_date) if raw_date is not None and all_day else None,
                "all_day": all_day if raw_date is not None else False,
                "date_source": "sqlite" if raw_date is not None else "none",
            }
            reminders[uid_seed] = item

    return list(reminders.values())


def run_reminders_sqlite_lists_export(config: dict[str, Any]) -> list[dict[str, Any]]:
    list_filter = set(config["include_lists"])
    lists_by_key: dict[str, dict[str, Any]] = {}

    query = """
        SELECT
            l.Z_PK AS pk,
            l.ZNAME AS title,
            l.ZEXTERNALIDENTIFIER AS external_id,
            l.ZIDENTIFIER AS identifier
        FROM ZREMCDBASELIST l
        WHERE COALESCE(l.ZMARKEDFORDELETION, 0) = 0
          AND l.ZNAME IS NOT NULL
    """

    for path in list_reminders_sqlite_paths(config):
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            eprint(f"Skipping unreadable Reminders store {path}: {exc}")
            continue

        with connection:
            try:
                rows = connection.execute(query).fetchall()
            except sqlite3.Error as exc:
                eprint(f"Skipping incompatible Reminders store {path}: {exc}")
                continue

        account_id = path.stem.replace("Data-", "")
        for row in rows:
            title = str(row["title"] or "")
            if not title or (list_filter and title not in list_filter):
                continue
            list_id = str(row["external_id"] or blob_to_hex(row["identifier"]) or row["pk"])
            key = f"{account_id}:{list_id}:{title}"
            lists_by_key[key] = {
                "id": list_id,
                "title": title,
                "account_title": account_id,
                "account_id": account_id,
            }

    return sorted(lists_by_key.values(), key=lambda item: str(item.get("title") or ""))


def parse_rfc3339(value: str) -> dt.datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def format_rfc3339(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def reminder_uid(reminder: dict[str, Any]) -> str:
    raw_id = reminder.get("stable_id") or reminder.get("external_id") or reminder.get("id")
    if not raw_id:
        raw_id = canonical_json(
            {
                "title": reminder.get("title", ""),
                "list_id": reminder.get("list_id", ""),
                "due_at": reminder.get("due_at", ""),
            }
        )
    raw = "|".join(
        [
            str(reminder.get("account_id") or ""),
            str(reminder.get("list_id") or ""),
            str(raw_id),
        ]
    )
    return sha256_text(raw, 40)


def reminder_description(reminder: dict[str, Any], uid: str) -> str:
    parts: list[str] = []
    notes = str(reminder.get("notes") or "").strip()
    if notes:
        parts.append(notes)

    metadata = [
        "Synced from Apple Reminders.",
        f"List: {reminder.get('list_title') or '(unknown)'}",
        f"Source UID: {uid}",
    ]
    if parts:
        parts.append("")
    parts.extend(metadata)
    return "\n".join(parts)


def strip_tasks_sync_metadata(notes: str) -> str:
    lines = notes.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "Synced from Apple Reminders.":
            return "\n".join(lines[:index]).rstrip()
    return notes.rstrip()


def parse_tasks_sync_metadata(notes: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    lines = notes.splitlines()
    in_metadata = False

    for line in lines:
        stripped = line.strip()
        if stripped == "Synced from Apple Reminders.":
            in_metadata = True
            metadata[SYNC_MARKER_KEY] = SYNC_MARKER_VALUE
            continue
        if not in_metadata or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        metadata[key.strip().lower()] = value.strip()

    return metadata


def task_due_date(reminder: dict[str, Any]) -> str | None:
    if reminder.get("due_date"):
        try:
            return dt.date.fromisoformat(str(reminder["due_date"])[:10]).isoformat()
        except ValueError:
            return None
    due_at = reminder.get("due_at")
    if not due_at:
        return None
    try:
        return parse_rfc3339(str(due_at)).astimezone().date().isoformat()
    except ValueError:
        return None


def task_due_rfc3339(date_text: str | None) -> str | None:
    if not date_text:
        return None
    try:
        due_date = dt.date.fromisoformat(str(date_text)[:10]).isoformat()
    except ValueError:
        return None
    return f"{due_date}T00:00:00.000Z"


def task_body_for_patch(body: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    patch_body = dict(body)
    if "due" not in patch_body and existing and existing.get("due"):
        patch_body["due"] = None
    return patch_body


def task_has_current_source_digest(task: dict[str, Any], digest: str) -> bool:
    metadata = parse_tasks_sync_metadata(str(task.get("notes") or ""))
    return metadata.get("source digest") == digest


def task_material_without_due(material: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": material.get("title"),
        "notes": material.get("notes"),
        "status": material.get("status"),
    }


def task_only_due_differs(
    current_task: dict[str, Any],
    desired_body: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    current = task_change_material(current_task, reminder, config)
    desired = task_change_material(desired_body, reminder, config)
    return task_material_without_due(current) == task_material_without_due(desired) and current != desired


def task_matches_desired(
    current_task: dict[str, Any],
    desired_body: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    return task_change_digest(current_task, reminder, config) == task_change_digest(desired_body, reminder, config)


def reminder_tasks_notes(reminder: dict[str, Any], uid: str, digest: str) -> str:
    parts: list[str] = []
    notes = str(reminder.get("notes") or "").strip()
    if notes:
        parts.append(notes)

    metadata = [
        "Synced from Apple Reminders.",
        f"List: {reminder.get('list_title') or '(unknown)'}",
        f"Source UID: {uid}",
        f"Source Digest: {digest}",
    ]
    if parts:
        parts.append("")
    parts.extend(metadata)
    return "\n".join(parts)


def build_task(reminder: dict[str, Any], config: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    uid = reminder_uid(reminder)
    title = str(reminder.get("title") or "").strip() or "(Untitled reminder)"
    due_date = task_due_date(reminder)
    status = "completed" if reminder.get("is_completed") else "needsAction"
    body: dict[str, Any] = {
        "title": title,
        "status": status,
    }
    due = task_due_rfc3339(due_date)
    if due:
        body["due"] = due

    digest_material = {
        "task": {
            "title": title,
            "notes": str(reminder.get("notes") or "").strip(),
            "due_date": due_date,
            "status": status,
        },
        "source_modified_at": reminder.get("modified_at"),
        "source_completed_at": reminder.get("completed_at"),
    }
    digest = sha256_text(canonical_json(digest_material))
    body["notes"] = reminder_tasks_notes(reminder, uid, digest)
    return uid, body, digest


def build_desired_tasks(
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]], int]:
    exported_reminders = run_reminders_export(config)
    reminders = [
        reminder
        for reminder in exported_reminders
        if not reminder_is_undated(reminder)
        or list_sync_policy(config, str(reminder.get("list_title") or "Apple Reminders"))["sync_undated"]
    ]
    desired: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]] = {}
    skipped_invalid = 0

    if config.get("tasks_mirror_lists") and config.get("tasks_mirror_empty_lists"):
        for reminder_list in run_reminders_lists_export(config):
            title = str(reminder_list.get("title") or "").strip()
            if title:
                desired.setdefault(title, {})

    for reminder in reminders:
        try:
            uid, body, digest = build_task(reminder, config)
            list_title = str(reminder.get("list_title") or "Apple Reminders")
            desired.setdefault(list_title, {})[uid] = (body, digest, reminder)
        except (ValueError, KeyError) as exc:
            skipped_invalid += 1
            eprint(f"Skipping reminder: {exc}")

    return reminders, desired, skipped_invalid


def build_completed_tasks(
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]], int]:
    reminders = run_reminders_export(config, completed_only=True)
    completed: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]] = {}
    skipped_invalid = 0

    for reminder in reminders:
        try:
            uid, body, digest = build_task(reminder, config)
            list_title = str(reminder.get("list_title") or "Apple Reminders")
            completed.setdefault(list_title, {})[uid] = (body, digest, reminder)
        except (ValueError, KeyError) as exc:
            skipped_invalid += 1
            eprint(f"Skipping completed reminder: {exc}")

    return reminders, completed, skipped_invalid


def build_event(reminder: dict[str, Any], config: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    uid = reminder_uid(reminder)
    title = str(reminder.get("title") or "").strip() or "(Untitled reminder)"
    if config["prefix_list"] and reminder.get("list_title"):
        title = f"[{reminder['list_title']}] {title}"

    body: dict[str, Any] = {
        "summary": title,
        "description": reminder_description(reminder, uid),
        "transparency": config["transparency"],
        "extendedProperties": {
            "private": {
                SYNC_MARKER_KEY: SYNC_MARKER_VALUE,
                UID_KEY: uid,
            }
        },
    }

    if reminder.get("all_day"):
        due_date = reminder.get("due_date")
        if not due_date:
            raise ValueError(f"All-day reminder is missing due_date: {title}")
        start_date = dt.date.fromisoformat(str(due_date))
        end_date = start_date + dt.timedelta(days=1)
        body["start"] = {"date": start_date.isoformat()}
        body["end"] = {"date": end_date.isoformat()}
    else:
        due_at = reminder.get("due_at")
        if not due_at:
            raise ValueError(f"Timed reminder is missing due_at: {title}")
        start = parse_rfc3339(str(due_at))
        end = start + dt.timedelta(minutes=int(config["default_duration_minutes"]))
        body["start"] = {"dateTime": format_rfc3339(start)}
        body["end"] = {"dateTime": format_rfc3339(end)}

    popup_minutes = config.get("google_popup_minutes") or []
    if popup_minutes:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": int(minutes)} for minutes in popup_minutes],
        }
    else:
        body["reminders"] = {"useDefault": False}

    digest_material = {
        "event": body,
        "source_modified_at": reminder.get("modified_at"),
        "source_completed_at": reminder.get("completed_at"),
    }
    digest = sha256_text(canonical_json(digest_material))
    body["extendedProperties"]["private"][DIGEST_KEY] = digest
    return uid, body, digest


def build_desired_events(
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str, dict[str, Any]]], int]:
    reminders = run_reminders_export(config)
    desired: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {}
    skipped_invalid = 0

    for reminder in reminders:
        try:
            uid, body, digest = build_event(reminder, config)
            desired[uid] = (body, digest, reminder)
        except (ValueError, KeyError) as exc:
            skipped_invalid += 1
            eprint(f"Skipping reminder: {exc}")

    return reminders, desired, skipped_invalid


def private_props(event: dict[str, Any]) -> dict[str, str]:
    return event.get("extendedProperties", {}).get("private", {}) or {}


def index_existing_events(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_uid: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []

    for event in events:
        uid = private_props(event).get(UID_KEY)
        if not uid:
            continue
        if uid in by_uid:
            duplicates.append(event)
        else:
            by_uid[uid] = event
    return by_uid, duplicates


def index_existing_tasks(tasks: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_uid: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []

    for task in tasks:
        metadata = parse_tasks_sync_metadata(str(task.get("notes") or ""))
        if metadata.get(SYNC_MARKER_KEY) != SYNC_MARKER_VALUE:
            continue
        uid = metadata.get("source uid")
        if not uid:
            continue
        if uid in by_uid:
            duplicates.append(task)
        else:
            by_uid[uid] = task

    return by_uid, duplicates


def task_fingerprint(task_or_body: dict[str, Any]) -> str:
    due = str(task_or_body.get("due") or "")
    return canonical_json(
        {
            "title": str(task_or_body.get("title") or "").strip(),
            "due_date": due[:10] if len(due) >= 10 else "",
        }
    )


def task_title_due_material(task_or_body: dict[str, Any]) -> dict[str, Any]:
    due = str(task_or_body.get("due") or "")
    due_date = due[:10] if len(due) >= 10 else None
    return {
        "title": str(task_or_body.get("title") or "").strip() or "(Untitled reminder)",
        "due_date": due_date,
    }


def parse_optional_rfc3339(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return parse_rfc3339(str(value)).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def google_task_is_newer_than_reminder(task: dict[str, Any], reminder: dict[str, Any]) -> bool:
    google_updated = parse_optional_rfc3339(task.get("updated"))
    apple_modified = parse_optional_rfc3339(reminder.get("modified_at") or reminder.get("created_at"))
    if google_updated and apple_modified:
        return google_updated > apple_modified
    return bool(google_updated and not apple_modified)


def index_synced_tasks_by_fingerprint(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_fingerprint: dict[str, dict[str, Any]] = {}
    duplicate_fingerprints: set[str] = set()

    for task in tasks:
        metadata = parse_tasks_sync_metadata(str(task.get("notes") or ""))
        if metadata.get(SYNC_MARKER_KEY) != SYNC_MARKER_VALUE:
            continue
        fingerprint = task_fingerprint(task)
        if fingerprint in by_fingerprint:
            duplicate_fingerprints.add(fingerprint)
        else:
            by_fingerprint[fingerprint] = task

    for fingerprint in duplicate_fingerprints:
        by_fingerprint.pop(fingerprint, None)
    return by_fingerprint


def task_title_fingerprint(task_or_body: dict[str, Any]) -> str:
    return str(task_or_body.get("title") or "").strip()


def index_synced_tasks_by_title(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_title: dict[str, dict[str, Any]] = {}
    duplicate_titles: set[str] = set()

    for task in tasks:
        metadata = parse_tasks_sync_metadata(str(task.get("notes") or ""))
        if metadata.get(SYNC_MARKER_KEY) != SYNC_MARKER_VALUE:
            continue
        title = task_title_fingerprint(task)
        if title in by_title:
            duplicate_titles.add(title)
        else:
            by_title[title] = task

    for title in duplicate_titles:
        by_title.pop(title, None)
    return by_title


def task_source_uid(task: dict[str, Any]) -> str:
    metadata = parse_tasks_sync_metadata(str(task.get("notes") or ""))
    return str(metadata.get("source uid") or "")


def can_reuse_task_fallback(
    task: dict[str, Any],
    uid: str,
    desired: dict[str, tuple[dict[str, Any], str, dict[str, Any]]],
    tasklist_id: str,
    claimed_task_ids: set[tuple[str, str]],
) -> bool:
    task_id = str(task.get("id") or "")
    if task_id and (tasklist_id, task_id) in claimed_task_ids:
        return False
    source_uid = task_source_uid(task)
    return not source_uid or source_uid == uid or source_uid not in desired


def can_use_task_state_record(
    record: dict[str, Any],
    uid: str,
    desired: dict[str, tuple[dict[str, Any], str, dict[str, Any]]],
    tasklist_id: str,
    existing_by_id: dict[str, dict[str, Any]],
    claimed_task_ids: set[tuple[str, str]],
) -> bool:
    task_id = str(record.get("task_id") or "")
    if not task_id:
        return False
    if (tasklist_id, task_id) in claimed_task_ids:
        return False
    existing = existing_by_id.get(task_id)
    return not existing or can_reuse_task_fallback(existing, uid, desired, tasklist_id, claimed_task_ids)


def strip_sync_metadata(description: str) -> str:
    lines = description.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "Synced from Apple Reminders.":
            return "\n".join(lines[:index]).rstrip()
    return description.rstrip()


def event_summary_to_reminder_title(
    event_or_body: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> str:
    summary = str(event_or_body.get("summary") or "").strip()
    if config.get("prefix_list") and reminder.get("list_title"):
        prefix = f"[{reminder['list_title']}] "
        if summary.startswith(prefix):
            return summary[len(prefix):].strip()
    return summary or "(Untitled reminder)"


def event_time_fields(event_or_body: dict[str, Any]) -> dict[str, Any]:
    start = event_or_body.get("start") or {}
    if "date" in start:
        return {
            "all_day": True,
            "due_date": str(start.get("date") or ""),
            "due_at": None,
        }

    due_at = str(start.get("dateTime") or "")
    if due_at:
        try:
            due_at = format_rfc3339(parse_rfc3339(due_at))
        except ValueError:
            pass

    return {
        "all_day": False,
        "due_date": None,
        "due_at": due_at,
    }


def google_change_material(
    event_or_body: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    material = {
        "title": event_summary_to_reminder_title(event_or_body, reminder, config),
        "notes": strip_sync_metadata(str(event_or_body.get("description") or "")),
    }
    material.update(event_time_fields(event_or_body))
    return material


def google_change_digest(
    event_or_body: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> str:
    return sha256_text(canonical_json(google_change_material(event_or_body, reminder, config)))


def google_event_to_reminder_operation(
    event: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    material = google_change_material(event, reminder, config)
    operation = {
        "stable_id": reminder.get("stable_id") or reminder.get("external_id") or reminder.get("id"),
        "id": reminder.get("id"),
        "title": material["title"],
        "notes": material["notes"],
        "all_day": bool(material["all_day"]),
    }
    if material["all_day"]:
        operation["due_date"] = material["due_date"]
    else:
        operation["due_at"] = material["due_at"]
    return operation


def task_change_material(
    task_or_body: dict[str, Any],
    reminder: dict[str, Any],
    _config: dict[str, Any],
) -> dict[str, Any]:
    due = str(task_or_body.get("due") or "")
    due_date = due[:10] if len(due) >= 10 else None
    return {
        "title": str(task_or_body.get("title") or "").strip() or "(Untitled reminder)",
        "notes": strip_tasks_sync_metadata(str(task_or_body.get("notes") or "")),
        "status": str(task_or_body.get("status") or "needsAction"),
        "all_day": bool(due_date),
        "due_date": due_date,
    }


def task_change_digest(
    task_or_body: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> str:
    return sha256_text(canonical_json(task_change_material(task_or_body, reminder, config)))


def google_task_to_reminder_operation(
    task: dict[str, Any],
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    material = task_change_material(task, reminder, config)
    operation = {
        "stable_id": reminder.get("stable_id") or reminder.get("external_id") or reminder.get("id"),
        "id": reminder.get("id"),
        "title": material["title"],
        "notes": material["notes"],
    }
    if material["due_date"]:
        operation["all_day"] = True
        operation["due_date"] = material["due_date"]
    else:
        operation["clear_due"] = True
    if task.get("status") == "completed":
        operation["complete"] = True
    return operation


def google_task_to_new_reminder_operation(
    task: dict[str, Any],
    list_title: str,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    material = task_change_material(task, {}, config)
    due_date = material.get("due_date")
    if not due_date and not list_sync_policy(config, list_title)["sync_undated"]:
        return None

    operation: dict[str, Any] = {
        "create": True,
        "list_title": list_title,
        "title": material["title"],
        "notes": material["notes"],
    }
    if due_date:
        operation["all_day"] = True
        operation["due_date"] = due_date
    return operation


def reminder_from_created_task_result(
    result: dict[str, Any],
    task: dict[str, Any],
    list_title: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    material = task_change_material(task, {}, config)
    return {
        "id": result.get("id") or result.get("stable_id") or "",
        "external_id": result.get("external_id") or "",
        "stable_id": result.get("stable_id") or result.get("external_id") or result.get("id") or "",
        "title": material["title"],
        "notes": material["notes"],
        "list_title": result.get("list_title") or list_title,
        "list_id": result.get("list_id") or "",
        "account_title": result.get("account_title") or "",
        "account_id": result.get("account_id") or "",
        "priority": 0,
        "is_completed": False,
        "created_at": result.get("created_at"),
        "modified_at": result.get("modified_at"),
        "completed_at": None,
        "due_at": None,
        "due_date": material["due_date"],
        "all_day": bool(material["due_date"]),
        "date_source": "due" if material["due_date"] else "none",
    }


def unique_desired_by_task_fingerprint(
    desired: dict[str, tuple[dict[str, Any], str, dict[str, Any]]],
) -> dict[str, tuple[str, dict[str, Any], str, dict[str, Any]]]:
    by_fingerprint: dict[str, tuple[str, dict[str, Any], str, dict[str, Any]]] = {}
    duplicate_fingerprints: set[str] = set()

    for uid, (body, digest, reminder) in desired.items():
        fingerprint = task_fingerprint(body)
        if fingerprint in by_fingerprint:
            duplicate_fingerprints.add(fingerprint)
        else:
            by_fingerprint[fingerprint] = (uid, body, digest, reminder)

    for fingerprint in duplicate_fingerprints:
        by_fingerprint.pop(fingerprint, None)
    return by_fingerprint


def run_reminders_apply(config: dict[str, Any], operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    apply_path = expand_path(config["reminders_apply_path"])
    if not apply_path.exists():
        raise SystemExit(f"Reminders apply helper not found: {apply_path}")
    if not shutil.which("swift"):
        raise SystemExit("Swift is not installed or not on PATH. Google-to-Apple sync requires Swift/EventKit.")

    process = subprocess.run(
        ["swift", str(apply_path)],
        input=json.dumps(operations, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        hint = (
            "\nIf this is a macOS privacy error, run the command once from Terminal and allow "
            "Reminders access in System Settings > Privacy & Security > Reminders."
        )
        raise SystemExit(
            "Failed to update Apple Reminders from Google.\n"
            f"Command: swift {apply_path}\n"
            f"{process.stderr.strip()}"
            f"{hint}"
        )

    try:
        payload = json.loads(process.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Reminders apply helper returned invalid JSON: {exc}\n{process.stdout}") from exc

    if not isinstance(payload, list):
        raise SystemExit("Reminders apply helper did not return a JSON array.")
    return payload


def plan_google_changes_to_reminders(
    config: dict[str, Any],
    calendar_id: str,
    desired: dict[str, tuple[dict[str, Any], str, dict[str, Any]]],
    existing_by_uid: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    if not config["bidirectional"]:
        return {
            "operations": [],
            "initialized": 0,
            "conflicts": [],
            "blocked_uids": set(),
            "source_controlled": set(),
            "actions": [],
        }

    operations: list[dict[str, Any]] = []
    initialized = 0
    conflicts: list[str] = []
    blocked_uids: set[str] = set()
    source_controlled: set[str] = set()
    actions: list[dict[str, Any]] = []

    for uid, (body, digest, reminder) in desired.items():
        existing = existing_by_uid.get(uid)
        if not existing:
            continue

        record = state_record(state, calendar_id, uid)
        current_google_digest = google_change_digest(existing, reminder, config)
        expected_google_digest = google_change_digest(body, reminder, config)
        stored_google_digest = record.get("google_digest") if record else None

        if stored_google_digest:
            google_changed = current_google_digest != stored_google_digest
        else:
            google_changed = current_google_digest != expected_google_digest
            if not google_changed:
                initialized += 1

        if not google_changed:
            continue

        apple_changed = bool(record and record.get("digest") and record.get("digest") != digest)
        title = str(existing.get("summary") or body.get("summary") or uid)
        if apple_changed:
            if current_google_digest == expected_google_digest:
                initialized += 1
                continue
            conflicts.append(title)
            blocked_uids.add(uid)
            continue

        operations.append(google_event_to_reminder_operation(existing, reminder, config))
        source_controlled.add(uid)
        actions.append(planned_mutation("apple_reminders", "update", [calendar_id, uid]))

    return {
        "operations": operations,
        "initialized": initialized,
        "conflicts": conflicts,
        "blocked_uids": blocked_uids,
        "source_controlled": source_controlled,
        "actions": actions,
    }


def apply_google_changes_to_reminders(
    config: dict[str, Any],
    calendar_id: str,
    desired: dict[str, tuple[dict[str, Any], str, dict[str, Any]]],
    existing_by_uid: dict[str, dict[str, Any]],
    state: dict[str, Any],
    dry_run: bool,
    *,
    planned: dict[str, Any] | None = None,
) -> tuple[int, int, int, set[str]]:
    preview = planned or plan_google_changes_to_reminders(
        config,
        calendar_id,
        desired,
        existing_by_uid,
        state,
    )
    operations = preview["operations"]
    initialized = int(preview["initialized"])
    conflicts = list(preview["conflicts"])
    blocked_uids = set(preview["blocked_uids"])

    if conflicts:
        for title in conflicts:
            print(f"Skipping bidirectional conflict: {title}")

    if not operations:
        if initialized:
            print(f"Bidirectional state initialized for {initialized} synced events.")
        return 0, initialized, len(conflicts), blocked_uids

    if dry_run:
        for operation in operations:
            print(f"DRY-RUN apply Google change to Apple Reminders: {operation.get('title')}")
        return len(operations), initialized, len(conflicts), blocked_uids

    results = run_reminders_apply(config, operations)
    missing = [result for result in results if result.get("status") == "missing"]
    for result in missing:
        eprint(f"Could not find Apple reminder for Google change: {result.get('stable_id')}")
    applied = sum(1 for result in results if result.get("status") in ("updated", "deleted", "completed"))
    if applied:
        print(f"Applied Google changes to Apple Reminders: {applied}")
    if initialized:
        print(f"Bidirectional state initialized for {initialized} synced events.")
    return applied, initialized, len(conflicts), blocked_uids


def plan_google_task_changes_to_reminders(
    config: dict[str, Any],
    desired_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]],
    tasklists: dict[str, str],
    existing_by_list: dict[str, dict[str, dict[str, Any]]],
    all_tasks_by_list: dict[str, list[dict[str, Any]]],
    deleted_tasks_by_list: dict[str, list[dict[str, Any]]],
    state: dict[str, Any],
    *,
    allow_deletes: bool,
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    operation_contexts: list[tuple[str, Any]] = []
    google_patches: list[tuple[str, str, str, dict[str, Any], str, dict[str, Any], dict[str, Any]]] = []
    initialized = 0
    conflicts: list[str] = []
    blocked: set[tuple[str, str]] = set()
    source_controlled: set[tuple[str, str]] = set()
    actions: list[dict[str, Any]] = []
    seen_deleted_uids: set[tuple[str, str]] = set()

    if allow_deletes:
        for list_title, tasks in deleted_tasks_by_list.items():
            if not list_allows_google_to_apple(config, list_title):
                continue
            if not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                continue
            tasklist_id = tasklists.get(list_title)
            if not tasklist_id:
                continue
            desired = desired_by_list.get(list_title, {})
            active_by_uid = existing_by_list.get(list_title, {})

            for task in tasks:
                if not task.get("deleted"):
                    continue
                metadata = parse_tasks_sync_metadata(str(task.get("notes") or ""))
                if metadata.get(SYNC_MARKER_KEY) != SYNC_MARKER_VALUE:
                    continue
                uid = metadata.get("source uid")
                if not uid or (list_title, uid) in seen_deleted_uids:
                    continue
                if uid in active_by_uid:
                    continue

                record = task_state_record(state, tasklist_id, uid)
                # A tombstone is an edge from the currently tracked task, not
                # a permanent ban on this source UID. Successful deletion in
                # either direction removes that mapping. Replaying its old
                # tombstone would delete an Apple reminder the user restored.
                if not record or not task.get("id") or record.get("task_id") != task.get("id"):
                    continue
                desired_item = desired.get(uid)
                reminder = desired_item[2] if desired_item else {}
                if record and desired_item and record.get("digest") and record.get("digest") != desired_item[1]:
                    policy = list_sync_policy(config, list_title)
                    if policy["direction"] != "google_to_apple":
                        if policy["conflict_policy"] == "newer_wins":
                            if not google_task_is_newer_than_reminder(task, reminder):
                                continue
                        else:
                            title = str(task.get("title") or reminder.get("title") or record.get("title") or uid)
                            conflicts.append(title)
                            blocked.add((list_title, uid))
                            continue

                stable_id = (
                    reminder.get("stable_id")
                    or reminder.get("external_id")
                    or reminder.get("id")
                    or (record or {}).get("source_stable_id")
                )
                if not stable_id:
                    continue

                title = str(task.get("title") or reminder.get("title") or (record or {}).get("title") or uid)
                operations.append(
                    {
                        "stable_id": stable_id,
                        "id": stable_id,
                        "title": title,
                        "delete": True,
                    }
                )
                operation_contexts.append(("delete", (list_title, tasklist_id, uid)))
                blocked.add((list_title, uid))
                source_controlled.add((list_title, uid))
                seen_deleted_uids.add((list_title, uid))
                actions.append(
                    planned_mutation(
                        "apple_reminders",
                        "delete",
                        [list_title, tasklist_id, uid, stable_id],
                        destructive=True,
                    )
                )

    for list_title, desired in desired_by_list.items():
        if not list_allows_google_to_apple(config, list_title):
            continue
        tasklist_id = tasklists.get(list_title)
        if not tasklist_id:
            continue
        existing_by_uid = existing_by_list.get(list_title, {})
        for uid, (body, digest, reminder) in desired.items():
            existing = existing_by_uid.get(uid)
            if not existing:
                continue

            record = task_state_record(state, tasklist_id, uid)
            current_google_digest = task_change_digest(existing, reminder, config)
            expected_google_digest = task_change_digest(body, reminder, config)
            stored_google_digest = record.get("google_digest") if record else None

            if stored_google_digest:
                google_changed = current_google_digest != stored_google_digest
            else:
                google_changed = current_google_digest != expected_google_digest
                if not google_changed:
                    initialized += 1

            if not google_changed:
                continue

            apple_changed = bool(record and record.get("digest") and record.get("digest") != digest)
            title = str(existing.get("title") or body.get("title") or uid)
            policy = list_sync_policy(config, list_title)
            google_is_authoritative = policy["direction"] == "google_to_apple"
            preserve_concurrent_apple_due = bool(
                apple_changed
                and not google_is_authoritative
                and body.get("due")
                and not existing.get("due")
            )

            # Google Tasks can temporarily omit a generated due field. Also,
            # when both sides changed, a newer Google title or note must not
            # erase a date that was concurrently added in Apple Reminders.
            if task_only_due_differs(existing, body, reminder, config) and (
                task_has_current_source_digest(existing, digest) or preserve_concurrent_apple_due
            ):
                continue

            # A completed Google task can outlive its local state record while the
            # matching Apple reminder is completed. If the reminder is reopened
            # later, resolve that recordless completion mismatch with the configured
            # conflict policy instead of always completing the Apple reminder again.
            reopened_without_state = bool(
                not record
                and str(existing.get("status") or "needsAction") == "completed"
                and str(body.get("status") or "needsAction") == "needsAction"
            )
            if reopened_without_state and not google_is_authoritative:
                if policy["conflict_policy"] != "newer_wins":
                    conflicts.append(title)
                    blocked.add((list_title, uid))
                    continue
                if not google_task_is_newer_than_reminder(existing, reminder):
                    continue

            if apple_changed:
                if current_google_digest == expected_google_digest:
                    initialized += 1
                    continue
                if google_is_authoritative or policy["conflict_policy"] == "newer_wins":
                    if google_is_authoritative or google_task_is_newer_than_reminder(existing, reminder):
                        operation = google_task_to_reminder_operation(existing, reminder, config)
                        if preserve_concurrent_apple_due:
                            operation.pop("clear_due", None)
                        if operation.get("complete") and not list_allows_delete_propagation(
                            config,
                            list_title,
                            safety_allows_deletes=allow_deletes,
                        ):
                            blocked.add((list_title, uid))
                            source_controlled.add((list_title, uid))
                            continue
                        operations.append(operation)
                        operation_contexts.append(("update", None))
                        if not preserve_concurrent_apple_due or operation.get("complete"):
                            source_controlled.add((list_title, uid))
                        operation_name = "complete" if operation.get("complete") else "update"
                        actions.append(
                            planned_mutation(
                                "apple_reminders",
                                operation_name,
                                [list_title, tasklist_id, uid],
                                destructive=operation_name == "complete",
                            )
                        )
                    continue
                conflicts.append(title)
                blocked.add((list_title, uid))
                continue

            operation = google_task_to_reminder_operation(existing, reminder, config)
            if operation.get("complete") and not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                blocked.add((list_title, uid))
                source_controlled.add((list_title, uid))
                continue
            operations.append(operation)
            operation_contexts.append(("update", None))
            source_controlled.add((list_title, uid))
            operation_name = "complete" if operation.get("complete") else "update"
            actions.append(
                planned_mutation(
                    "apple_reminders",
                    operation_name,
                    [list_title, tasklist_id, uid],
                    destructive=operation_name == "complete",
                )
            )

    if config.get("tasks_import_unsynced"):
        for list_title, tasks in all_tasks_by_list.items():
            if not list_allows_google_to_apple(config, list_title):
                continue
            tasklist_id = tasklists.get(list_title)
            if not tasklist_id:
                continue
            desired = desired_by_list.get(list_title, {})
            desired_by_fingerprint = unique_desired_by_task_fingerprint(desired)
            existing_by_uid = existing_by_list.get(list_title, {})

            for task in tasks:
                task_id = str(task.get("id") or "")
                if not task_id:
                    continue
                if str(task.get("status") or "needsAction") == "completed":
                    continue

                metadata = parse_tasks_sync_metadata(str(task.get("notes") or ""))
                if metadata.get(SYNC_MARKER_KEY) == SYNC_MARKER_VALUE:
                    continue

                matched = desired_by_fingerprint.get(task_fingerprint(task))
                if matched:
                    uid, body, digest, reminder = matched
                    if uid in existing_by_uid:
                        continue
                    google_patches.append((list_title, tasklist_id, uid, body, digest, reminder, task))
                    source_controlled.add((list_title, uid))
                    actions.append(
                        planned_mutation(
                            "google_tasks",
                            "attach",
                            [list_title, tasklist_id, uid, task_id],
                        )
                    )
                    continue

                operation = google_task_to_new_reminder_operation(task, list_title, config)
                if not operation:
                    continue

                operations.append(operation)
                operation_contexts.append(("create", (list_title, tasklist_id, task)))
                actions.append(
                    planned_mutation(
                        "apple_reminders",
                        "create",
                        [list_title, tasklist_id, task_id],
                    )
                )
                actions.append(
                    planned_mutation(
                        "google_tasks",
                        "attach_after_create",
                        [list_title, tasklist_id, task_id],
                    )
                )

    return {
        "operations": operations,
        "operation_contexts": operation_contexts,
        "google_patches": google_patches,
        "initialized": initialized,
        "conflicts": conflicts,
        "blocked": blocked,
        "source_controlled": source_controlled,
        "actions": actions,
    }


def apply_google_task_changes_to_reminders(
    config: dict[str, Any],
    client: GoogleTasksClient,
    desired_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]],
    tasklists: dict[str, str],
    existing_by_list: dict[str, dict[str, dict[str, Any]]],
    all_tasks_by_list: dict[str, list[dict[str, Any]]],
    deleted_tasks_by_list: dict[str, list[dict[str, Any]]],
    state: dict[str, Any],
    dry_run: bool,
    *,
    planned: dict[str, Any] | None = None,
    allow_deletes: bool = True,
) -> tuple[int, int, int, set[tuple[str, str]]]:
    preview = planned or plan_google_task_changes_to_reminders(
        config,
        desired_by_list,
        tasklists,
        existing_by_list,
        all_tasks_by_list,
        deleted_tasks_by_list,
        state,
        allow_deletes=allow_deletes,
    )
    operations = preview["operations"]
    operation_contexts = preview["operation_contexts"]
    google_patches = preview["google_patches"]
    initialized = int(preview["initialized"])
    conflicts = list(preview["conflicts"])
    blocked = set(preview["blocked"])

    if conflicts:
        for title in conflicts:
            print(f"Skipping bidirectional conflict: {title}")

    if not operations and not google_patches:
        if initialized:
            print(f"Bidirectional state initialized for {initialized} synced tasks.")
        return 0, initialized, len(conflicts), blocked

    if dry_run:
        for operation in operations:
            if operation.get("delete"):
                print(f"DRY-RUN delete Apple Reminder from deleted Google Tasks: {operation.get('title')}")
            elif operation.get("create"):
                print(f"DRY-RUN create Apple Reminder from Google Tasks: {operation.get('title')}")
            else:
                print(f"DRY-RUN apply Google Tasks change to Apple Reminders: {operation.get('title')}")
        for _list_title, _tasklist_id, _uid, body, _digest, _reminder, task in google_patches:
            print(f"DRY-RUN attach existing Google Tasks item to Apple Reminder: {task.get('title') or body.get('title')}")
        return len(operations) + len(google_patches), initialized, len(conflicts), blocked

    applied = 0
    for list_title, tasklist_id, uid, body, digest, reminder, task in google_patches:
        task_id = str(task.get("id") or "")
        patched = client.patch_task(tasklist_id, task_id, task_body_for_patch(body, task))
        save_task_state(state, tasklist_id, uid, patched, digest, str(body.get("title") or uid), reminder, config, list_title)
        blocked.add((list_title, uid))
        applied += 1

    results = run_reminders_apply(config, operations) if operations else []
    for result, (kind, _context) in zip(results, operation_contexts):
        if result.get("status") == "missing" and kind != "delete":
            eprint(f"Could not find Apple reminder for Google Tasks change: {result.get('stable_id')}")
    missing_lists = [result for result in results if result.get("status") == "missing_list"]
    for result in missing_lists:
        eprint(f"Could not find Apple Reminders list for Google Tasks import: {result.get('list_title')}")

    for result, (kind, context) in zip(results, operation_contexts):
        status = str(result.get("status") or "")
        if kind == "delete" and context:
            list_title, tasklist_id, uid = context
            if status in ("deleted", "missing"):
                remove_task_state_record(state, tasklist_id, uid)
                blocked.add((list_title, uid))
                if status == "deleted":
                    applied += 1
            continue

        if status in ("updated", "deleted", "completed"):
            applied += 1
            continue
        if status != "created" or not context:
            continue

        list_title, tasklist_id, task = context
        task_id = str(task.get("id") or "")
        reminder = reminder_from_created_task_result(result, task, list_title, config)
        uid, body, digest = build_task(reminder, config)
        patched = client.patch_task(tasklist_id, task_id, task_body_for_patch(body, task))
        save_task_state(state, tasklist_id, uid, patched, digest, str(body.get("title") or uid), reminder, config, list_title)
        blocked.add((list_title, uid))
        applied += 1

    if applied:
        print(f"Applied Google Tasks changes to Apple Reminders: {applied}")
    if initialized:
        print(f"Bidirectional state initialized for {initialized} synced tasks.")
    return applied, initialized, len(conflicts), blocked


def save_event_state(
    state: dict[str, Any],
    calendar_id: str,
    uid: str,
    event: dict[str, Any],
    digest: str,
    title: str,
    reminder: dict[str, Any],
    config: dict[str, Any],
) -> None:
    state["events"][state_key(calendar_id, uid)] = {
        "calendar_id": calendar_id,
        "event_id": event["id"],
        "digest": digest,
        "google_digest": google_change_digest(event, reminder, config),
        "source_stable_id": reminder.get("stable_id") or reminder.get("external_id") or reminder.get("id"),
        "source_modified_at": reminder.get("modified_at"),
        "apple_completed": bool(reminder.get("is_completed")),
        "apple_recurring": bool(reminder.get("is_recurring")),
        "title": title,
        "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def save_task_state(
    state: dict[str, Any],
    tasklist_id: str,
    uid: str,
    task: dict[str, Any],
    digest: str,
    title: str,
    reminder: dict[str, Any],
    config: dict[str, Any],
    list_title: str,
) -> None:
    state.setdefault("tasks", {})[target_state_key(tasklist_id, uid)] = {
        "tasklist_id": tasklist_id,
        "tasklist_title": list_title,
        "task_id": task["id"],
        "digest": digest,
        "google_digest": task_change_digest(task, reminder, config),
        "source_stable_id": reminder.get("stable_id") or reminder.get("external_id") or reminder.get("id"),
        "source_modified_at": reminder.get("modified_at"),
        "apple_completed": bool(reminder.get("is_completed")),
        "apple_recurring": bool(reminder.get("is_recurring")),
        "title": title,
        "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def state_record(state: dict[str, Any], calendar_id: str, uid: str) -> dict[str, Any] | None:
    record = state["events"].get(state_key(calendar_id, uid))
    return record if isinstance(record, dict) else None


def task_state_record(state: dict[str, Any], tasklist_id: str, uid: str) -> dict[str, Any] | None:
    tasks_state = state.setdefault("tasks", {})
    record = tasks_state.get(target_state_key(tasklist_id, uid))
    return record if isinstance(record, dict) else None


def remove_state_record(state: dict[str, Any], calendar_id: str, uid: str) -> None:
    state["events"].pop(state_key(calendar_id, uid), None)


def remove_task_state_record(state: dict[str, Any], tasklist_id: str, uid: str) -> None:
    state.setdefault("tasks", {}).pop(target_state_key(tasklist_id, uid), None)


def cmd_init_config(args: argparse.Namespace) -> None:
    path = expand_path(args.config)
    if path.exists() and not args.force:
        raise SystemExit(f"Config already exists: {path}\nUse --force to overwrite it.")
    write_json_atomic(path, default_config())
    print(f"Wrote config template: {path}")


def cmd_auth(args: argparse.Namespace) -> None:
    config = load_config(args)
    try:
        run_auth_flow(config)
    except OAuthCallbackTimeout as exc:
        raise SystemExit(str(exc)) from exc
    write_sync_status(
        config,
        {
            "state": "auth_refreshed",
            "last_manual_auth_completed_at": utc_now_text(),
            **auto_reauth_failure_epoch_reset_updates(),
            "last_error": "",
        },
    )


def cmd_gcloud_login(args: argparse.Namespace) -> None:
    config = load_config(args)
    if not shutil.which("gcloud"):
        raise SystemExit("gcloud is not installed. Install Google Cloud CLI first.")

    credentials_path = expand_path(config["credentials_path"])
    if not credentials_path.exists():
        raise SystemExit(
            "Google Cloud CLI cannot request Google Calendar/Tasks scopes with its built-in OAuth client.\n"
            "Create a Desktop OAuth client JSON and save it here first:\n"
            f"  {credentials_path}\n"
            "Then rerun:\n"
            "  python3 icloud_reminders_google_sync.py gcloud-login\n"
            "Google's own gcloud help documents this requirement for non-Google-Cloud scopes."
        )

    command = [
        "gcloud",
        "auth",
        "application-default",
        "login",
        f"--client-id-file={credentials_path}",
        f"--scopes={GCLOUD_LOGIN_SCOPES}",
    ]
    print("Running Google Cloud ADC login. Complete the browser login when it opens.")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)

    adc_path = expand_path(config["adc_credentials_path"])
    if adc_path.exists():
        write_sync_status(
            config,
            {
                "state": "auth_refreshed",
                "last_manual_auth_completed_at": utc_now_text(),
                **auto_reauth_failure_epoch_reset_updates(),
                "last_error": "",
            },
        )
        print(f"ADC credentials ready: {adc_path}")
    else:
        raise SystemExit(f"gcloud finished but ADC credentials were not found: {adc_path}")


def cmd_export(args: argparse.Namespace) -> None:
    config = load_config(args)
    reminders = run_reminders_export(config)
    print(json.dumps(reminders, indent=2, ensure_ascii=False, sort_keys=True))


def launch_agent_loaded() -> bool | None:
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        return None
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def local_status_snapshot(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    status_path = expand_path(config["status_path"])
    if not status_path.exists():
        return "missing", {}
    try:
        status = read_json(status_path)
    except (OSError, json.JSONDecodeError):
        return "unreadable", {}
    if not isinstance(status, dict):
        return "unreadable", {}
    return "available", status


def safe_status_time(value: Any) -> str:
    parsed = parse_status_time(value)
    return parsed.isoformat(timespec="seconds") if parsed else "unknown"


def doctor_next_step(
    *,
    config_exists: bool,
    swift_ready: bool,
    helpers_ready: bool,
    runtime_ready: bool,
    auth_material_ready: bool,
    launch_agent_installed: bool,
    agent_loaded: bool | None,
    status_result: str,
    status_state: str,
) -> str:
    if not config_exists:
        return "Install a reviewed main release with setup-new-mac.sh; setup writes the private config."
    if not swift_ready:
        return "Install Xcode Command Line Tools, then rerun doctor."
    if not helpers_ready or not runtime_ready:
        return "Reinstall the reviewed main release before loading the LaunchAgent."
    if status_state == "auth_prompt_open":
        return "Complete the open Google browser authorization, then rerun doctor."
    if not auth_material_ready or status_state in {"auth_required", "auth_timeout"}:
        return "Run the stable runtime's gcloud-login command once, then rerun doctor."
    if not launch_agent_installed:
        return "Run reviewed setup to install the user LaunchAgent; do not point it at this checkout."
    if agent_loaded is False:
        return "Inspect the private error log, resolve the reported cause, then reload the user LaunchAgent."
    if status_result == "unreadable":
        return "Back up the private status file, repair its JSON, then rerun doctor."
    if status_result == "missing":
        return "Run a reviewed first dry-run with --no-delete-stale before enabling normal synchronization."
    if status_state == "blocked_mutation_plan":
        return "Review a new dry-run and its mutation counts before approving any write."
    if status_state == "account_binding_required":
        return (
            "Run the stable runtime's manage command and choose reconnect. "
            "It will verify Google first, back up private state, and rebuild with --no-delete-stale only after confirmation."
        )
    if status_state == "dry_run_ok":
        return "Review the dry-run counts, then run the first live sync with --no-delete-stale before relying on scheduled synchronization."
    if status_state == "failed":
        return "Inspect the private error log and rerun doctor; stored error details are intentionally hidden here."
    if status_state == "running":
        return "Wait for the current cycle to finish, then rerun doctor."
    if status_state in {"ok", "auth_refreshed"}:
        return "No recovery action is currently indicated."
    return "Review the private LaunchAgent log and rerun doctor after the next scheduled cycle."


def run_online_doctor_checks(config: dict[str, Any]) -> None:
    print("Online Google checks:")
    result = check_google_connection(config)
    if result["state"] == "ok":
        print("  Google auth refresh: ok")
        if config["target_service"] == "tasks":
            print("  Google Tasks API: ok")
    elif result["state"] == "auth_required":
        if result.get("source") == "google_api":
            print(f"  Google API: {result['message']}")
        else:
            print("  Google auth refresh: authorization required")
    elif result["state"] == "api_error":
        print(f"  Google API: {result['message']}")
    else:
        print("  Google checks: unavailable; review the private log and retry later")


def check_google_connection(config: dict[str, Any]) -> dict[str, Any]:
    """Refresh the selected credential and prove the configured Google API is reachable.

    The returned message is deliberately sanitized. Callers may show it in a local
    management UI without exposing Google response bodies, tokens, or local paths.
    """
    try:
        token = load_token(config)
        refreshed = refresh_token_with_fallback(config, token)
        tasklist_count = 0
        if config["target_service"] == "tasks":
            client = GoogleTasksClient(config)
            client.token = refreshed
            tasklist_count = len(client.list_tasklists())
        identity = fetch_google_account_identity(str(refreshed.get("access_token") or ""))
        return {
            "state": "ok",
            "source": "google_api",
            "tasklist_count": tasklist_count,
            **identity,
            "message": "Google connection is healthy.",
        }
    except AuthenticationRequired:
        return {
            "state": "auth_required",
            "source": "oauth",
            "tasklist_count": 0,
            "message": "Google authorization has expired or was revoked.",
        }
    except OAuthTokenError as exc:
        return {"state": "auth_required", "source": "oauth", "tasklist_count": 0, "message": str(exc)}
    except GoogleApiError as exc:
        state = "auth_required" if exc.status in (401, 403) else "api_error"
        return {
            "state": state,
            "source": "google_api",
            "tasklist_count": 0,
            "message": str(exc),
        }
    except SystemExit:
        return {
            "state": "auth_required",
            "source": "oauth",
            "tasklist_count": 0,
            "message": "Google authorization material is missing or invalid.",
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {
            "state": "unavailable",
            "source": "network",
            "tasklist_count": 0,
            "message": "Google could not be reached safely. Retry after checking the network.",
        }


def fetch_google_account_identity(access_token: str) -> dict[str, str]:
    """Return display-only Google identity details without persisting raw claims."""
    if not access_token:
        return {"account_email": "", "account_fingerprint": ""}
    request = urllib.request.Request(
        OAUTH_USERINFO_URL,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return {"account_email": "", "account_fingerprint": ""}
    if not isinstance(payload, dict):
        return {"account_email": "", "account_fingerprint": ""}
    email = " ".join(str(payload.get("email") or "").split())
    subject = str(payload.get("sub") or "").strip()
    return {
        "account_email": email,
        "account_fingerprint": sha256_text(f"google-openid-sub\n{subject}", 12) if subject else "",
    }


def cmd_doctor(args: argparse.Namespace) -> None:
    try:
        config = load_config(args)
    except (OSError, json.JSONDecodeError, SystemExit, TypeError, ValueError):
        print("Local prerequisites:")
        print("  Configuration: invalid")
        print("Online Google checks: skipped (local read-only mode)")
        print("Next step: Repair the private config or reinstall a reviewed main release, then rerun doctor.")
        return

    config_exists = expand_path(config["_config_path"]).exists()
    adc_path = expand_path(config["adc_credentials_path"])
    local_token_path = expand_path(config["token_path"])
    runtime_current = Path.home() / ".local/share" / APP_NAME / "current"
    runtime_ready = runtime_current.is_symlink() and (runtime_current / Path(__file__).name).is_file()
    launch_agent_path = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    agent_loaded = launch_agent_loaded()
    helpers_ready = expand_path(config["reminders_exporter_path"]).exists() and expand_path(
        config["reminders_apply_path"]
    ).exists()
    auth_material_ready = adc_path.exists() or local_token_path.exists()
    checks = [
        ("Configuration", config_exists),
        ("Swift", shutil.which("swift") is not None),
        ("Reminders exporter", expand_path(config["reminders_exporter_path"]).exists()),
        ("Reminders apply helper", expand_path(config["reminders_apply_path"]).exists()),
        ("Google OAuth client JSON", expand_path(config["credentials_path"]).exists()),
        ("gcloud ADC credentials", adc_path.exists()),
        ("Local OAuth token", local_token_path.exists()),
        ("Stable runtime release", runtime_ready),
        ("LaunchAgent file", launch_agent_path.is_file()),
    ]
    print("Local prerequisites:")
    for label, ok in checks:
        print(f"  {label}: {'ok' if ok else 'missing'}")
    if agent_loaded is None:
        print("  LaunchAgent loaded: unavailable on this platform")
    else:
        print(f"  LaunchAgent loaded: {'ok' if agent_loaded else 'not loaded'}")

    status_result, status = local_status_snapshot(config)
    raw_state = str(status.get("state") or "unknown")
    status_state = raw_state if raw_state in KNOWN_STATUS_STATES else "unknown"
    print("Local sync status:")
    print(f"  Status file: {status_result}")
    if status_result == "available":
        print(f"  State: {status_state}")
        print(f"  Updated: {safe_status_time(status.get('updated_at'))}")
        print(f"  Last successful sync: {safe_status_time(status.get('last_success_at'))}")
        try:
            failure_count = max(0, int(status.get("consecutive_failures") or 0))
        except (TypeError, ValueError):
            failure_count = 0
        print(f"  Consecutive failures: {failure_count}")
        if status.get("last_error"):
            print("  Last sync error: recorded; details hidden to protect credentials and local paths")

    if getattr(args, "online", False):
        run_online_doctor_checks(config)
    else:
        print("Online Google checks: skipped (local read-only mode)")
        print("  Use doctor --online only when a Google token refresh is acceptable.")

    next_step = doctor_next_step(
        config_exists=config_exists,
        swift_ready=shutil.which("swift") is not None,
        helpers_ready=helpers_ready,
        runtime_ready=runtime_ready,
        auth_material_ready=auth_material_ready,
        launch_agent_installed=launch_agent_path.is_file(),
        agent_loaded=agent_loaded,
        status_result=status_result,
        status_state=status_state,
    )
    print(f"Next step: {next_step}")


class ManagementActionError(RuntimeError):
    pass


def collect_management_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    config_exists = expand_path(config["_config_path"]).is_file()
    runtime_current = Path.home() / ".local/share" / APP_NAME / "current"
    stable_entrypoint = runtime_current / Path(__file__).name
    runtime_ready = runtime_current.is_symlink() and stable_entrypoint.is_file()
    running_from_stable_runtime = runtime_ready and Path(__file__).resolve() == stable_entrypoint.resolve()
    launch_agent_path = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    status_result, status = local_status_snapshot(config)
    raw_state = str(status.get("state") or "unknown")
    try:
        failure_count = max(0, int(status.get("consecutive_failures") or 0))
    except (TypeError, ValueError):
        failure_count = 0
    return {
        "config_exists": config_exists,
        "swift_ready": shutil.which("swift") is not None,
        "helpers_ready": expand_path(config["reminders_exporter_path"]).is_file()
        and expand_path(config["reminders_apply_path"]).is_file(),
        "oauth_client_ready": expand_path(config["credentials_path"]).is_file(),
        "auth_material_ready": expand_path(config["adc_credentials_path"]).is_file()
        or expand_path(config["token_path"]).is_file(),
        "runtime_ready": runtime_ready,
        "running_from_stable_runtime": running_from_stable_runtime,
        "launch_agent_path": launch_agent_path,
        "launch_agent_installed": launch_agent_path.is_file(),
        "agent_loaded": launch_agent_loaded(),
        "status_result": status_result,
        "status_state": raw_state if raw_state in KNOWN_STATUS_STATES else "unknown",
        "last_success_at": status.get("last_success_at"),
        "updated_at": status.get("updated_at"),
        "failure_count": failure_count,
    }


def management_condition(snapshot: dict[str, Any]) -> tuple[str, str, str]:
    if not snapshot["config_exists"] or not snapshot["runtime_ready"] or not snapshot["helpers_ready"]:
        return (
            "setup_required",
            "설치 또는 실행 파일이 완전하지 않습니다.",
            "검토된 main 릴리스를 setup-new-mac.sh로 다시 설치하세요.",
        )
    state = str(snapshot["status_state"])
    if state == "account_binding_required":
        return (
            "account_binding_required",
            "Apple/Google 계정 연결 기준이 달라져 동기화가 안전 정지되었습니다.",
            "'Google 연결 복구'를 선택하면 백업 후 삭제 전파 없이 상태를 재구축할 수 있습니다.",
        )
    if state in {"auth_required", "auth_timeout"} or not snapshot["auth_material_ready"]:
        return (
            "auth_required",
            "Google 로그인이 만료되었거나 인증 파일을 사용할 수 없습니다.",
            "'Google 연결 복구'를 선택해 브라우저 로그인을 완료하세요.",
        )
    if state == "blocked_mutation_plan":
        return (
            "mutation_blocked",
            "대량 완료·삭제 계획이 안전 기준을 넘어 동기화가 멈췄습니다.",
            "수동 dry-run 결과를 검토하기 전에는 쓰기를 재개하지 마세요.",
        )
    if snapshot["agent_loaded"] is False:
        return (
            "agent_stopped",
            "백그라운드 동기화가 실행 중이 아닙니다.",
            "원인을 해결한 뒤 '백그라운드 다시 시작'을 선택하세요.",
        )
    if snapshot["status_result"] == "missing":
        return (
            "first_sync_required",
            "아직 동기화 결과가 없습니다.",
            "'Google 연결 복구'에서 삭제 전파 없는 첫 동기화를 진행하세요.",
        )
    if snapshot["status_result"] == "unreadable":
        return (
            "status_invalid",
            "상태 파일을 읽을 수 없습니다.",
            "비공개 상태 파일을 백업한 뒤 검토된 릴리스를 다시 설치하세요.",
        )
    if state == "failed":
        return (
            "failed",
            "최근 동기화가 실패했습니다.",
            "Google 연결 확인 후 비공개 로그에서 원인을 확인하세요.",
        )
    if state == "running":
        return ("running", "지금 동기화가 진행 중입니다.", "잠시 뒤 상태를 다시 확인하세요.")
    if state == "ok":
        return ("healthy", "Apple 미리 알림과 Google Tasks 동기화가 정상입니다.", "현재 필요한 조치는 없습니다.")
    if state in {"dry_run_ok", "auth_refreshed"}:
        return (
            "attention",
            "연결 또는 안전 점검은 끝났지만 정상 동기화 완료를 아직 확인하지 못했습니다.",
            "'Google 연결 복구'를 계속 진행하거나 다음 자동 동기화 후 다시 확인하세요.",
        )
    return ("unknown", "현재 상태를 하나로 확정할 수 없습니다.", "Google 연결 확인을 실행해 범위를 좁히세요.")


def management_local_time(value: Any) -> str:
    parsed = parse_status_time(value)
    if not parsed:
        return "기록 없음"
    return parsed.astimezone().isoformat(timespec="seconds")


def print_management_summary(config: dict[str, Any]) -> dict[str, Any]:
    snapshot = collect_management_snapshot(config)
    condition, headline, action = management_condition(snapshot)
    print("\nGoogle Tasks 동기화 관리")
    print("=" * 32)
    print(f"판정: {headline}")
    print(f"상태 코드: {condition}")
    print(f"최근 성공: {management_local_time(snapshot['last_success_at'])}")
    print(f"연속 실패: {snapshot['failure_count']}회")
    if snapshot["agent_loaded"] is None:
        print("백그라운드: 이 환경에서는 확인할 수 없음")
    else:
        print(f"백그라운드: {'실행 중' if snapshot['agent_loaded'] else '중지됨'}")
    print(f"Google 인증 파일: {'준비됨' if snapshot['auth_material_ready'] else '없음'}")
    print(f"권장 조치: {action}")
    return snapshot


def management_confirm(args: argparse.Namespace, prompt: str) -> bool:
    if bool(getattr(args, "yes", False)):
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def backup_management_files(config: dict[str, Any], reason: str) -> Path:
    def private_path(value: str | os.PathLike[str]) -> Path:
        return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))

    config_path = private_path(config["_config_path"])
    backup_root = config_path.parent / "backups"
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"{stamp}-{reason}-{os.getpid()}"
    backup_dir.mkdir(parents=True, mode=0o700)
    os.chmod(backup_dir, 0o700)

    private_paths = {
        "config.json": config_path,
        "credentials.json": private_path(config["credentials_path"]),
        "gcloud-application-default-credentials.json": private_path(config["adc_credentials_path"]),
        "token.json": private_path(config["token_path"]),
        "state.json": private_path(config["state_path"]),
        "status.json": private_path(config["status_path"]),
    }
    copied = 0
    for backup_name, source in private_paths.items():
        if not source.is_file() or source.is_symlink():
            continue
        destination = backup_dir / backup_name
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)
        copied += 1
    if copied == 0:
        backup_dir.rmdir()
        raise ManagementActionError("백업할 비공개 설정이나 상태 파일을 찾지 못했습니다.")
    return backup_dir


def stop_management_agent(snapshot: dict[str, Any]) -> bool:
    was_loaded = snapshot["agent_loaded"] is True
    if not was_loaded:
        return False
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        raise ManagementActionError("이 환경에서는 macOS 백그라운드 작업을 중지할 수 없습니다.")
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(snapshot["launch_agent_path"])],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    if result.returncode != 0 and launch_agent_loaded() is not False:
        raise ManagementActionError("백그라운드 동기화를 안전하게 중지하지 못했습니다.")
    return True


def start_management_agent(snapshot: dict[str, Any]) -> None:
    if not snapshot["launch_agent_installed"]:
        raise ManagementActionError("LaunchAgent 파일이 없습니다. 검토된 릴리스를 다시 설치하세요.")
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        raise ManagementActionError("이 환경에서는 macOS 백그라운드 작업을 시작할 수 없습니다.")
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(snapshot["launch_agent_path"])],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    if result.returncode != 0 or launch_agent_loaded() is not True:
        raise ManagementActionError("백그라운드 동기화를 다시 시작하지 못했습니다.")


def run_management_safe_sync(config: dict[str, Any], *, dry_run: bool) -> None:
    safe_config = dict(config)
    safe_config["delete_stale"] = False
    safe_config["_disable_delete_propagation"] = True
    safe_config["_mutation_plan_approval"] = ""
    safe_config["auto_reauth_browser"] = False
    with sync_lock(safe_config, wait=True) as acquired:
        if not acquired:
            raise ManagementActionError("다른 동기화가 실행 중입니다. 잠시 뒤 다시 시도하세요.")
        write_sync_status(
            safe_config,
            {"state": "running", "last_start_at": utc_now_text(), "last_error": "", "mutation_plan": None},
        )
        try:
            run_sync(safe_config, dry_run=dry_run)
        except AccountBindingRequired as exc:
            write_sync_status(
                safe_config,
                {"state": "account_binding_required", "last_end_at": utc_now_text(), "last_error": str(exc)},
            )
            raise
        except Exception as exc:
            write_sync_status(
                safe_config,
                {"state": "failed", "last_end_at": utc_now_text(), "last_error": str(exc)},
            )
            raise
        write_sync_status(
            safe_config,
            {
                "state": "dry_run_ok" if dry_run else "ok",
                "last_end_at": utc_now_text(),
                "last_success_at": utc_now_text(),
                **auto_reauth_failure_epoch_reset_updates(),
                "last_error": "",
                "consecutive_failures": 0,
            },
        )


def reminders_account_titles(config: dict[str, Any]) -> list[str]:
    records = run_reminders_lists_export(config)
    return sorted(
        {
            str(record.get("account_title") or "").strip()
            for record in records
            if isinstance(record, dict) and str(record.get("account_title") or "").strip()
        }
    )


def authorize_google_for_management(config: dict[str, Any], args: argparse.Namespace) -> None:
    if shutil.which("gcloud"):
        try:
            cmd_gcloud_login(argparse.Namespace(config=config["_config_path"], adc_credentials_path=None))
            return
        except SystemExit as exc:
            print(f"gcloud 로그인에 실패했습니다: {exc}")
            if not management_confirm(args, "브라우저 직접 로그인을 대신 시도할까요?"):
                raise ManagementActionError("Google 로그인이 완료되지 않았습니다.") from exc
    cmd_auth(argparse.Namespace(config=config["_config_path"], credentials_path=None))


def archive_state_for_rebuild(config: dict[str, Any], backup_dir: Path) -> bool:
    state_path = expand_path(config["state_path"])
    if not state_path.exists():
        return False
    destination = backup_dir / "state.active-before-rebuild.json"
    if destination.exists():
        raise ManagementActionError("상태 백업 대상이 이미 있어 기존 상태를 이동하지 않았습니다.")
    state_path.replace(destination)
    os.chmod(destination, 0o600)
    return True


def google_account_display(result: dict[str, Any]) -> str:
    email = str(result.get("account_email") or "").strip()
    if email:
        return email
    fingerprint = str(result.get("account_fingerprint") or "").strip()
    if fingerprint:
        return f"비공개 식별자 {fingerprint}"
    return "확인할 수 없음"


def management_reconnect(config: dict[str, Any], args: argparse.Namespace) -> None:
    snapshot = print_management_summary(config)
    if not all(
        [
            snapshot["config_exists"],
            snapshot["runtime_ready"],
            snapshot["running_from_stable_runtime"],
            snapshot["helpers_ready"],
            snapshot["oauth_client_ready"],
            snapshot["launch_agent_installed"],
        ]
    ):
        raise ManagementActionError("재연결 전에 검토된 main 릴리스를 다시 설치해야 합니다.")
    if not management_confirm(
        args,
        "비공개 상태를 백업하고 백그라운드 동기화를 잠시 멈춘 뒤 연결 복구를 시작할까요?",
    ):
        print("변경 없이 취소했습니다.")
        return

    backup_dir = backup_management_files(config, "reconnect")
    print(f"비공개 백업 완료: {backup_dir}")
    was_loaded = stop_management_agent(snapshot)
    state_path = expand_path(config["state_path"])
    state_existed = state_path.is_file()
    state_archived = False
    completed = False

    try:
        online = check_google_connection(config)
        if online["state"] == "ok":
            print(
                f"Google 연결: 정상 ({google_account_display(online)}, "
                f"Tasks 목록 {online['tasklist_count']}개 확인)"
            )
        elif online["state"] == "auth_required":
            print("Google 로그인이 필요합니다. 브라우저 인증을 시작합니다.")
            authorize_google_for_management(config, args)
            online = check_google_connection(config)
            if online["state"] != "ok":
                raise ManagementActionError(f"재로그인 후에도 Google 연결을 확인하지 못했습니다: {online['message']}")
            print(
                f"Google 재연결: 정상 ({google_account_display(online)}, "
                f"Tasks 목록 {online['tasklist_count']}개 확인)"
            )
        else:
            raise ManagementActionError(
                f"네트워크 또는 Google 일시 장애로 연결을 판정하지 못했습니다. 나중에 다시 시도하세요: {online['message']}"
            )

        print("\n삭제·완료 전파를 끈 안전 dry-run을 실행합니다.")
        try:
            run_management_safe_sync(config, dry_run=True)
        except AccountBindingRequired:
            if not online.get("account_email"):
                print("현재 Google 자격 증명에는 계정 확인 권한이 없어 안전하게 계정을 식별할 수 없습니다.")
                print("계정 식별 권한을 포함해 Google 로그인을 한 번 갱신합니다.")
                authorize_google_for_management(config, args)
                online = check_google_connection(config)
                if online["state"] != "ok" or not online.get("account_email"):
                    raise ManagementActionError("Google 계정을 식별하지 못해 상태 재구축을 중단합니다.")
            account_titles = reminders_account_titles(config)
            print("\nGoogle 인증은 정상이지만 저장된 계정 연결 기준과 현재 계정 기준이 다릅니다.")
            print(f"현재 Apple 미리 알림 계정: {', '.join(account_titles) if account_titles else '확인할 수 없음'}")
            print(f"현재 Google 계정: {google_account_display(online)}")
            print("기존 상태는 이미 비공개 백업에 보존되어 있습니다.")
            if not management_confirm(
                args,
                "표시된 Apple 계정과 방금 확인한 Google 계정이 의도한 조합이라면 상태 맵을 안전하게 재구축할까요?",
            ):
                raise ManagementActionError("계정 확인이 취소되어 백그라운드 동기화를 중지 상태로 유지합니다.")
            state_archived = archive_state_for_rebuild(config, backup_dir)
            print("기존 활성 상태를 백업으로 옮겼습니다. Google/Apple 항목은 삭제하지 않았습니다.")
            run_management_safe_sync(config, dry_run=True)

        needs_live_rebuild = state_archived or not state_existed
        if needs_live_rebuild:
            print("\n위 dry-run 결과는 삭제·완료 전파가 모두 차단된 재구축 계획입니다.")
            if not management_confirm(args, "이 계획으로 새 상태 맵을 저장할까요?"):
                raise ManagementActionError("새 상태 맵 저장이 취소되어 백그라운드 동기화를 중지 상태로 유지합니다.")
            run_management_safe_sync(config, dry_run=False)

        start_management_agent(snapshot)
        completed = True
        print("\n복구 완료: Google 연결과 안전 동기화를 확인했고 백그라운드 작업을 다시 시작했습니다.")
        print_management_summary(config)
    finally:
        if not completed and was_loaded and not state_archived:
            try:
                start_management_agent(snapshot)
                print("기존 활성 상태는 바꾸지 않았고 백그라운드 작업을 원래대로 다시 시작했습니다.")
            except ManagementActionError:
                print("주의: 백그라운드 작업을 자동으로 복원하지 못했습니다.")


def management_restart(config: dict[str, Any], args: argparse.Namespace) -> None:
    snapshot = print_management_summary(config)
    if not snapshot["running_from_stable_runtime"]:
        raise ManagementActionError("재시작은 설치된 안정 실행 릴리스의 관리 도구에서만 허용됩니다.")
    condition = management_condition(snapshot)[0]
    if condition in {"account_binding_required", "auth_required", "mutation_blocked"}:
        raise ManagementActionError("원인을 해결하지 않은 재시작은 도움이 되지 않습니다. 먼저 'Google 연결 복구'를 실행하세요.")
    if not management_confirm(args, "백그라운드 동기화를 다시 시작할까요?"):
        print("변경 없이 취소했습니다.")
        return
    if snapshot["agent_loaded"] is True:
        stop_management_agent(snapshot)
    start_management_agent(snapshot)
    print("백그라운드 동기화를 다시 시작했습니다.")


def management_online_check(config: dict[str, Any]) -> bool:
    print_management_summary(config)
    print("\nGoogle 인증 토큰을 갱신하고 Tasks API를 확인합니다.")
    result = check_google_connection(config)
    if result["state"] == "ok":
        print(
            f"Google 연결: 정상 ({google_account_display(result)}, "
            f"Tasks 목록 {result['tasklist_count']}개 확인)"
        )
        return True
    print(f"Google 연결: 확인 필요 ({result['message']})")
    return False


def run_management_action(config: dict[str, Any], args: argparse.Namespace, action: str) -> bool:
    if action == "status":
        print_management_summary(config)
        return True
    if action == "check":
        return management_online_check(config)
    if action == "reconnect":
        management_reconnect(config, args)
        return True
    if action == "restart":
        management_restart(config, args)
        return True
    raise ManagementActionError(f"지원하지 않는 관리 작업입니다: {action}")


def cmd_manage(args: argparse.Namespace) -> None:
    try:
        config = load_config(args)
    except (OSError, json.JSONDecodeError, SystemExit, TypeError, ValueError) as exc:
        raise SystemExit("비공개 설정을 읽을 수 없습니다. 검토된 main 릴리스를 다시 설치하세요.") from exc

    action = str(getattr(args, "action", "menu") or "menu")
    if action != "menu":
        try:
            ok = run_management_action(config, args, action)
        except (
            ManagementActionError,
            AccountBindingRequired,
            AuthenticationRequired,
            MutationPlanApprovalRequired,
        ) as exc:
            raise SystemExit(f"관리 작업 중단: {exc}") from exc
        if not ok:
            raise SystemExit(1)
        return

    while True:
        print_management_summary(config)
        print("\n1. 현재 상태 보기 (읽기 전용)")
        print("2. Google 연결 실제 확인 (토큰 갱신 가능)")
        print("3. Google 연결 복구 및 안전 재연결")
        print("4. 백그라운드 다시 시작")
        print("0. 종료")
        try:
            choice = input("선택: ").strip()
        except EOFError:
            choice = "0"
        action_by_choice = {"1": "status", "2": "check", "3": "reconnect", "4": "restart"}
        if choice == "0":
            print("관리 도구를 종료합니다.")
            return
        selected = action_by_choice.get(choice)
        if not selected:
            print("0~4 중 하나를 선택하세요.")
            continue
        try:
            run_management_action(config, args, selected)
        except (ManagementActionError, AccountBindingRequired, AuthenticationRequired) as exc:
            print(f"\n관리 작업 중단: {exc}")
        except SystemExit as exc:
            print(f"\n관리 작업 중단: {exc}")
        input("\nEnter를 누르면 메뉴로 돌아갑니다.")


def inspect_tasklists_for_desired(
    client: GoogleTasksClient,
    desired_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]],
    config: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    if not config["tasks_mirror_lists"]:
        tasklist_id = client.resolve_tasklist_id()
        title = str(config.get("tasks_list_title") or "Google Tasks")
        return ({list_title: tasklist_id for list_title in desired_by_list} or {title: tasklist_id}, [])

    existing_lists = client.list_tasklists()
    by_title: dict[str, str] = {}
    for tasklist in existing_lists:
        title = str(tasklist.get("title") or "")
        if title and title not in by_title:
            by_title[title] = str(tasklist["id"])

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for list_title in sorted(desired_by_list):
        if list_title in by_title:
            resolved[list_title] = by_title[list_title]
            continue

        if not list_allows_apple_to_google(config, list_title):
            continue

        if not config["tasks_create_missing_lists"]:
            raise SystemExit(f"Google Tasks list does not exist: {list_title}")
        missing.append(list_title)
        resolved[list_title] = f"planned:{sha256_text(list_title, 24)}"

    return resolved, missing


def materialize_missing_tasklists(
    client: GoogleTasksClient,
    tasklists: dict[str, str],
    missing: list[str],
    *,
    dry_run: bool,
) -> dict[str, str]:
    resolved = dict(tasklists)
    for list_title in missing:
        if dry_run:
            print(f"DRY-RUN create Google Tasks list: {list_title}")
            continue
        tasklist = client.insert_tasklist(list_title)
        resolved[list_title] = str(tasklist["id"])
        print(f"Created Google Tasks list: {list_title}")
    return resolved


def list_task_snapshot(client: GoogleTasksClient, tasklist_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks = client.list_tasks(tasklist_id, show_deleted=True)
    active_tasks = [task for task in tasks if not task.get("deleted")]
    deleted_tasks = [task for task in tasks if task.get("deleted")]
    return active_tasks, deleted_tasks


def get_active_task_by_id(client: GoogleTasksClient, tasklist_id: str, task_id: str) -> dict[str, Any] | None:
    try:
        task = client.get_task(tasklist_id, task_id)
    except GoogleApiError as exc:
        if exc.status not in (404, 410):
            raise
        return None
    return None if task.get("deleted") else task


def google_tasks_title_due_consistency_issues(
    client: GoogleTasksClient,
    desired_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]],
    tasklists: dict[str, str],
    blocked: set[tuple[str, str]],
    recently_synced_task_ids: dict[tuple[str, str], str],
) -> tuple[list[str], int]:
    issues: list[str] = []
    verified = 0

    for list_title, desired in desired_by_list.items():
        tasklist_id = tasklists[list_title]
        if tasklist_id.startswith("dry-run:"):
            continue
        tasks, _deleted_tasks = list_task_snapshot(client, tasklist_id)
        existing_by_uid, _duplicates = index_existing_tasks(tasks)

        for uid, (body, _digest, _reminder) in desired.items():
            if (list_title, uid) in blocked:
                continue

            task = existing_by_uid.get(uid)
            expected = task_title_due_material(body)
            task_id = recently_synced_task_ids.get((tasklist_id, uid))
            if not task and task_id:
                task = get_active_task_by_id(client, tasklist_id, task_id)
            if not task:
                issues.append(f"[{list_title}] missing Google task for {expected['title']}")
                continue

            actual = task_title_due_material(task)
            if actual != expected and task_id:
                refreshed = get_active_task_by_id(client, tasklist_id, task_id)
                if refreshed:
                    task = refreshed
                    actual = task_title_due_material(task)
            if actual != expected:
                issues.append(
                    f"[{list_title}] {expected['title']}: "
                    f"title/due mismatch expected={expected} actual={actual}"
                )
                continue

            verified += 1

    return issues, verified


def verify_google_tasks_title_due_consistency(
    client: GoogleTasksClient,
    desired_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]],
    tasklists: dict[str, str],
    blocked: set[tuple[str, str]],
    recently_synced_task_ids: dict[tuple[str, str], str] | None = None,
    retry_attempts: int = 1,
    retry_delay_seconds: float = 0.0,
) -> int:
    attempts = max(1, retry_attempts)
    delay_seconds = max(0.0, retry_delay_seconds)
    task_ids = recently_synced_task_ids or {}
    issues: list[str] = []
    verified = 0

    for attempt in range(1, attempts + 1):
        issues, verified = google_tasks_title_due_consistency_issues(
            client,
            desired_by_list,
            tasklists,
            blocked,
            task_ids,
        )
        if not issues:
            print(f"Google Tasks title/due consistency verified: {verified}")
            return verified
        if attempt < attempts and delay_seconds:
            time.sleep(delay_seconds)

    if issues:
        shown = "\n".join(f"- {issue}" for issue in issues[:10])
        remaining = len(issues) - 10
        suffix = f"\n... and {remaining} more" if remaining > 0 else ""
        raise SystemExit(f"Google Tasks title/due consistency check failed:\n{shown}{suffix}")

    return verified


def plan_google_task_outbound_mutations(
    config: dict[str, Any],
    desired_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]],
    tasklists: dict[str, str],
    existing_by_list: dict[str, dict[str, dict[str, Any]]],
    existing_fallback_by_list: dict[str, dict[str, dict[str, Any]]],
    existing_title_by_list: dict[str, dict[str, dict[str, Any]]],
    existing_id_by_list: dict[str, dict[str, dict[str, Any]]],
    duplicates_by_list: dict[str, list[dict[str, Any]]],
    state: dict[str, Any],
    blocked: set[tuple[str, str]],
    completed_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]],
    *,
    allow_deletes: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    desired_keys: set[tuple[str, str]] = set()
    claimed_task_ids: set[tuple[str, str]] = set()

    for list_title, desired in desired_by_list.items():
        if not list_allows_apple_to_google(config, list_title):
            continue
        tasklist_id = tasklists.get(list_title)
        if not tasklist_id:
            continue
        existing_by_uid = existing_by_list.get(list_title, {})
        existing_by_fingerprint = existing_fallback_by_list.get(list_title, {})
        existing_by_title = existing_title_by_list.get(list_title, {})
        existing_by_id = existing_id_by_list.get(list_title, {})
        for uid, (body, digest, reminder) in desired.items():
            desired_keys.add((tasklist_id, uid))
            if (list_title, uid) in blocked:
                continue

            existing = existing_by_uid.get(uid)
            matched_old_uid = None
            if not existing:
                fallback = existing_by_fingerprint.get(task_fingerprint(body))
                if fallback and can_reuse_task_fallback(fallback, uid, desired, tasklist_id, claimed_task_ids):
                    existing = fallback
                    matched_old_uid = task_source_uid(existing)
            if not existing:
                fallback = existing_by_title.get(task_title_fingerprint(body))
                if fallback and can_reuse_task_fallback(fallback, uid, desired, tasklist_id, claimed_task_ids):
                    existing = fallback
                    matched_old_uid = task_source_uid(existing)

            record = task_state_record(state, tasklist_id, uid)
            if not record and matched_old_uid:
                record = task_state_record(state, tasklist_id, matched_old_uid)
            if record and not can_use_task_state_record(
                record,
                uid,
                desired,
                tasklist_id,
                existing_by_id,
                claimed_task_ids,
            ):
                record = None
            task_id = existing.get("id") if existing else record.get("task_id") if record else None
            existing_metadata = parse_tasks_sync_metadata(str(existing.get("notes") or "")) if existing else {}
            existing_digest = existing_metadata.get("source digest") if existing else record.get("digest") if record else None
            existing_matches_desired = bool(existing and task_matches_desired(existing, body, reminder, config))
            needs_update = bool(
                task_id
                and (
                    existing_digest != digest
                    or not existing_matches_desired
                    or bool(matched_old_uid and matched_old_uid != uid)
                    or (existing and existing.get("status") == "completed")
                )
            )
            if task_id:
                claimed_task_ids.add((tasklist_id, str(task_id)))
            if needs_update:
                actions.append(planned_mutation("google_tasks", "update", [list_title, tasklist_id, uid, task_id]))
            elif not task_id:
                actions.append(planned_mutation("google_tasks", "create", [list_title, tasklist_id, uid]))

    if allow_deletes:
        stale: dict[tuple[str, str], dict[str, Any]] = {}
        for key, record in list(state.setdefault("tasks", {}).items()):
            if not isinstance(record, dict):
                continue
            tasklist_id = str(record.get("tasklist_id") or "")
            list_title = str(record.get("tasklist_title") or "")
            uid = key.split(":", 1)[1] if ":" in key else ""
            task_id = str(record.get("task_id") or "")
            if not list_allows_apple_to_google(config, list_title):
                continue
            if not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                continue
            if (tasklist_id, task_id) in claimed_task_ids:
                continue
            if (list_title, uid) in blocked:
                continue
            if tasklist_id and uid and (tasklist_id, uid) not in desired_keys and task_id:
                stale[(tasklist_id, uid)] = {
                    "task_id": task_id,
                    "list_title": list_title,
                    "task": None,
                    "record": record,
                }

        for list_title, tasklist_id in tasklists.items():
            if not list_allows_apple_to_google(config, list_title):
                continue
            if not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                continue
            for uid, task in existing_by_list.get(list_title, {}).items():
                record = task_state_record(state, tasklist_id, uid)
                if str(task.get("status") or "") == "completed" and not record:
                    continue
                if (tasklist_id, str(task.get("id") or "")) in claimed_task_ids:
                    continue
                if (list_title, uid) in blocked:
                    continue
                if (tasklist_id, uid) not in desired_keys and task.get("id"):
                    stale[(tasklist_id, uid)] = {
                        "task_id": str(task["id"]),
                        "list_title": list_title,
                        "task": task,
                        "record": record,
                    }

        for (tasklist_id, uid), candidate in stale.items():
            task_id = str(candidate["task_id"])
            list_title = str(candidate.get("list_title") or "")
            task = candidate.get("task") if isinstance(candidate.get("task"), dict) else None
            record = candidate.get("record") if isinstance(candidate.get("record"), dict) else None
            completed_item = completed_by_list.get(list_title, {}).get(uid)
            if config["tasks_complete_stale"] and completed_item:
                _body, digest, _reminder = completed_item
                already_recorded = bool(
                    record
                    and record.get("apple_completed")
                    and record.get("digest") == digest
                    and task
                    and str(task.get("status") or "") == "completed"
                )
                already_completed = bool(task and str(task.get("status") or "") == "completed")
                if not already_recorded and not already_completed:
                    actions.append(
                        planned_mutation(
                            "google_tasks",
                            "complete",
                            [list_title, tasklist_id, uid, task_id],
                            destructive=True,
                        )
                    )
                continue
            actions.append(
                planned_mutation(
                    "google_tasks",
                    "delete",
                    [list_title, tasklist_id, uid, task_id],
                    destructive=True,
                )
            )

        for list_title, duplicates in duplicates_by_list.items():
            if not list_allows_apple_to_google(config, list_title):
                continue
            if not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                continue
            tasklist_id = tasklists.get(list_title)
            if not tasklist_id:
                continue
            for task in duplicates:
                task_id = str(task.get("id") or "")
                if task_id:
                    actions.append(
                        planned_mutation(
                            "google_tasks",
                            "dedupe_delete",
                            [list_title, tasklist_id, task_id],
                            destructive=True,
                        )
                    )

    return actions


def managed_google_task_population(
    tasklists: dict[str, str],
    tasks_by_list: dict[str, list[dict[str, Any]]],
    state: dict[str, Any],
) -> int:
    active_managed = sum(
        1
        for tasks in tasks_by_list.values()
        for task in tasks
        if parse_tasks_sync_metadata(str(task.get("notes") or "")).get(SYNC_MARKER_KEY) == SYNC_MARKER_VALUE
    )
    tasklist_ids = set(tasklists.values())
    state_managed = sum(
        1
        for record in state.setdefault("tasks", {}).values()
        if isinstance(record, dict) and str(record.get("tasklist_id") or "") in tasklist_ids
    )
    return max(active_managed, state_managed)


def plan_google_calendar_outbound_mutations(
    config: dict[str, Any],
    calendar_id: str,
    desired: dict[str, tuple[dict[str, Any], str, dict[str, Any]]],
    existing_by_uid: dict[str, dict[str, Any]],
    duplicates: list[dict[str, Any]],
    state: dict[str, Any],
    blocked_uids: set[str],
    *,
    allow_deletes: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for uid, (_body, digest, _reminder) in desired.items():
        if uid in blocked_uids:
            continue
        existing = existing_by_uid.get(uid)
        record = state_record(state, calendar_id, uid)
        event_id = existing.get("id") if existing else record.get("event_id") if record else None
        existing_digest = private_props(existing).get(DIGEST_KEY) if existing else record.get("digest") if record else None
        if event_id and existing_digest != digest:
            actions.append(planned_mutation("google_calendar", "update", [calendar_id, uid, event_id]))
        elif not event_id:
            actions.append(planned_mutation("google_calendar", "create", [calendar_id, uid]))

    if allow_deletes:
        desired_uids = set(desired)
        stale: dict[str, str] = {}
        for key, record in list(state["events"].items()):
            if not isinstance(record, dict) or record.get("calendar_id") != calendar_id:
                continue
            uid = key.split(":", 1)[1] if ":" in key else ""
            if uid and uid not in desired_uids and record.get("event_id"):
                stale[uid] = str(record["event_id"])
        for uid, event in existing_by_uid.items():
            if uid not in desired_uids and event.get("id"):
                stale[uid] = str(event["id"])
        for uid, event_id in stale.items():
            actions.append(
                planned_mutation(
                    "google_calendar",
                    "delete",
                    [calendar_id, uid, event_id],
                    destructive=True,
                )
            )
        for event in duplicates:
            event_id = str(event.get("id") or "")
            if event_id:
                actions.append(
                    planned_mutation(
                        "google_calendar",
                        "dedupe_delete",
                        [calendar_id, event_id],
                        destructive=True,
                    )
                )
    return actions


def managed_google_calendar_population(
    calendar_id: str,
    existing_by_uid: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> int:
    state_managed = sum(
        1
        for record in state["events"].values()
        if isinstance(record, dict) and record.get("calendar_id") == calendar_id
    )
    return max(len(existing_by_uid), state_managed)


def run_tasks_sync(config: dict[str, Any], dry_run: bool = False) -> None:
    reminders, desired_by_list, skipped_invalid = build_desired_tasks(config)

    print(f"Apple Reminders exported: {len(reminders)}")
    print(f"Google Tasks lists desired: {len(desired_by_list)}")
    print(f"Google Tasks desired: {sum(len(items) for items in desired_by_list.values())}")

    state_path = expand_path(config["state_path"])
    state = load_state(state_path)
    account_binding = resolve_sync_account_binding(config, reminders)
    bind_or_validate_sync_state_accounts(state, account_binding)
    config["_expected_google_credential_binding"] = account_binding["google"]
    client = GoogleTasksClient(config)
    tasklists, missing_tasklists = inspect_tasklists_for_desired(client, desired_by_list, config)
    tasks_by_list: dict[str, list[dict[str, Any]]] = {}
    deleted_tasks_by_list: dict[str, list[dict[str, Any]]] = {}
    existing_by_list: dict[str, dict[str, dict[str, Any]]] = {}
    existing_fallback_by_list: dict[str, dict[str, dict[str, Any]]] = {}
    existing_title_by_list: dict[str, dict[str, dict[str, Any]]] = {}
    existing_id_by_list: dict[str, dict[str, dict[str, Any]]] = {}
    duplicates_by_list: dict[str, list[dict[str, Any]]] = {}

    for list_title, tasklist_id in tasklists.items():
        if tasklist_id.startswith("planned:"):
            tasks: list[dict[str, Any]] = []
            deleted_tasks: list[dict[str, Any]] = []
        else:
            tasks, deleted_tasks = list_task_snapshot(client, tasklist_id)
        tasks_by_list[list_title] = tasks
        deleted_tasks_by_list[list_title] = deleted_tasks
        existing_by_uid, duplicates = index_existing_tasks(tasks)
        existing_by_list[list_title] = existing_by_uid
        existing_fallback_by_list[list_title] = index_synced_tasks_by_fingerprint(tasks)
        existing_title_by_list[list_title] = index_synced_tasks_by_title(tasks)
        existing_id_by_list[list_title] = {str(task["id"]): task for task in tasks if task.get("id")}
        duplicates_by_list[list_title] = duplicates

    source_count = sum(len(items) for items in desired_by_list.values())
    policy_list_titles = set(desired_by_list) | set(tasklists) | set(config.get("list_policies") or {})
    policy_list_titles.update(
        str(record.get("tasklist_title") or "")
        for record in state.setdefault("tasks", {}).values()
        if isinstance(record, dict)
    )
    delete_requested = any(
        list_allows_delete_propagation(config, list_title)
        for list_title in policy_list_titles
    )
    skip_stale = bool(delete_requested and not config["allow_empty_source_delete"] and source_count == 0)
    allow_deletes = bool(delete_requested and not skip_stale)
    completed_by_list: dict[str, dict[str, tuple[dict[str, Any], str, dict[str, Any]]]] = {}
    if allow_deletes and config["tasks_complete_stale"]:
        _completed_reminders, completed_by_list, completed_skipped = build_completed_tasks(config)
        skipped_invalid += completed_skipped

    bidirectional_plan = plan_google_task_changes_to_reminders(
        config,
        desired_by_list,
        tasklists,
        existing_by_list,
        tasks_by_list,
        deleted_tasks_by_list,
        state,
        allow_deletes=allow_deletes,
    )
    preflight_blocked = set(bidirectional_plan["blocked"]) | set(bidirectional_plan["source_controlled"])
    planned_actions = [
        planned_mutation("google_tasks", "create_list", list_title)
        for list_title in missing_tasklists
    ]
    planned_actions.extend(bidirectional_plan["actions"])
    planned_actions.extend(
        plan_google_task_outbound_mutations(
            config,
            desired_by_list,
            tasklists,
            existing_by_list,
            existing_fallback_by_list,
            existing_title_by_list,
            existing_id_by_list,
            duplicates_by_list,
            state,
            preflight_blocked,
            completed_by_list,
            allow_deletes=allow_deletes,
        )
    )
    population = max(source_count, managed_google_task_population(tasklists, tasks_by_list, state))
    mutation_plan = build_mutation_plan(planned_actions, population)
    enforce_mutation_plan(config, mutation_plan, dry_run=dry_run)

    tasklists = materialize_missing_tasklists(
        client,
        tasklists,
        missing_tasklists,
        dry_run=dry_run,
    )

    google_applied, bidir_initialized, bidir_conflicts, blocked = apply_google_task_changes_to_reminders(
        config,
        client,
        desired_by_list,
        tasklists,
        existing_by_list,
        tasks_by_list,
        deleted_tasks_by_list,
        state,
        dry_run,
        planned=bidirectional_plan,
        allow_deletes=allow_deletes,
    )
    if google_applied and not dry_run:
        time.sleep(1)
        reminders, desired_by_list, skipped_invalid = build_desired_tasks(config)
        print(f"Apple Reminders re-exported after Google Tasks changes: {len(reminders)}")
        print(f"Google Tasks desired: {sum(len(items) for items in desired_by_list.values())}")
        tasks_by_list = {}
        deleted_tasks_by_list = {}
        existing_by_list = {}
        existing_fallback_by_list = {}
        existing_title_by_list = {}
        existing_id_by_list = {}
        duplicates_by_list = {}
        for list_title, tasklist_id in tasklists.items():
            tasks, deleted_tasks = list_task_snapshot(client, tasklist_id)
            tasks_by_list[list_title] = tasks
            deleted_tasks_by_list[list_title] = deleted_tasks
            existing_by_uid, duplicates = index_existing_tasks(tasks)
            existing_by_list[list_title] = existing_by_uid
            existing_fallback_by_list[list_title] = index_synced_tasks_by_fingerprint(tasks)
            existing_title_by_list[list_title] = index_synced_tasks_by_title(tasks)
            existing_id_by_list[list_title] = {str(task["id"]): task for task in tasks if task.get("id")}
            duplicates_by_list[list_title] = duplicates

    inserted = updated = unchanged = completed = deleted = duplicate_deleted = 0
    desired_keys: set[tuple[str, str]] = set()
    claimed_task_ids: set[tuple[str, str]] = set()
    recently_synced_task_ids: dict[tuple[str, str], str] = {}

    for list_title, desired in desired_by_list.items():
        if not list_allows_apple_to_google(config, list_title):
            continue
        tasklist_id = tasklists.get(list_title)
        if not tasklist_id:
            continue
        existing_by_uid = existing_by_list.get(list_title, {})
        existing_by_fingerprint = existing_fallback_by_list.get(list_title, {})
        existing_by_title = existing_title_by_list.get(list_title, {})
        existing_by_id = existing_id_by_list.get(list_title, {})
        for uid, (body, digest, reminder) in desired.items():
            desired_keys.add((tasklist_id, uid))
            if (list_title, uid) in blocked:
                continue

            existing = existing_by_uid.get(uid)
            matched_old_uid = None
            if not existing:
                fallback = existing_by_fingerprint.get(task_fingerprint(body))
                if fallback and can_reuse_task_fallback(fallback, uid, desired, tasklist_id, claimed_task_ids):
                    existing = fallback
                    matched_old_uid = task_source_uid(existing)
            if not existing:
                fallback = existing_by_title.get(task_title_fingerprint(body))
                if fallback and can_reuse_task_fallback(fallback, uid, desired, tasklist_id, claimed_task_ids):
                    existing = fallback
                    matched_old_uid = task_source_uid(existing)

            record = task_state_record(state, tasklist_id, uid)
            if not record and matched_old_uid:
                record = task_state_record(state, tasklist_id, matched_old_uid)
            if record and not can_use_task_state_record(
                record,
                uid,
                desired,
                tasklist_id,
                existing_by_id,
                claimed_task_ids,
            ):
                record = None
            task_id = existing.get("id") if existing else record.get("task_id") if record else None
            existing_metadata = parse_tasks_sync_metadata(str(existing.get("notes") or "")) if existing else {}
            existing_digest = existing_metadata.get("source digest") if existing else record.get("digest") if record else None
            title = str(body.get("title") or reminder.get("title") or uid)
            existing_matches_desired = bool(existing and task_matches_desired(existing, body, reminder, config))
            needs_update = bool(
                task_id
                and (
                    existing_digest != digest
                    or not existing_matches_desired
                    or bool(matched_old_uid and matched_old_uid != uid)
                    or (existing and existing.get("status") == "completed")
                )
            )

            if dry_run:
                action = "update" if needs_update else "insert" if not task_id else "skip"
                print(f"DRY-RUN {action} task in [{list_title}]: {title}")
                if task_id:
                    claimed_task_ids.add((tasklist_id, str(task_id)))
                continue

            if existing and task_id and not needs_update:
                unchanged += 1
                save_task_state(state, tasklist_id, uid, existing, digest, title, reminder, config, list_title)
                if matched_old_uid and matched_old_uid != uid:
                    remove_task_state_record(state, tasklist_id, matched_old_uid)
                claimed_task_ids.add((tasklist_id, str(task_id)))
                recently_synced_task_ids[(tasklist_id, uid)] = str(task_id)
                continue

            if task_id:
                try:
                    task = client.patch_task(tasklist_id, task_id, task_body_for_patch(body, existing))
                    updated += 1
                except GoogleApiError as exc:
                    if exc.status not in (404, 410):
                        raise
                    task = client.insert_task(tasklist_id, body)
                    inserted += 1
            else:
                task = client.insert_task(tasklist_id, body)
                inserted += 1

            save_task_state(state, tasklist_id, uid, task, digest, title, reminder, config, list_title)
            if matched_old_uid and matched_old_uid != uid:
                remove_task_state_record(state, tasklist_id, matched_old_uid)
            claimed_task_ids.add((tasklist_id, str(task["id"])))
            recently_synced_task_ids[(tasklist_id, uid)] = str(task["id"])

    if skip_stale:
        print("Skipping stale completion/deletion because the Reminders source exported 0 items.")

    if allow_deletes:
        stale: dict[tuple[str, str], dict[str, Any]] = {}
        for key, record in list(state.setdefault("tasks", {}).items()):
            if not isinstance(record, dict):
                continue
            tasklist_id = str(record.get("tasklist_id") or "")
            list_title = str(record.get("tasklist_title") or "")
            uid = key.split(":", 1)[1] if ":" in key else ""
            task_id = str(record.get("task_id") or "")
            if not list_allows_apple_to_google(config, list_title):
                continue
            if not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                continue
            if (tasklist_id, task_id) in claimed_task_ids:
                continue
            if (list_title, uid) in blocked:
                continue
            if tasklist_id and uid and (tasklist_id, uid) not in desired_keys and record.get("task_id"):
                stale[(tasklist_id, uid)] = {
                    "task_id": str(record["task_id"]),
                    "list_title": list_title,
                    "task": None,
                    "record": record,
                }

        for list_title, tasklist_id in tasklists.items():
            if not list_allows_apple_to_google(config, list_title):
                continue
            if not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                continue
            for uid, task in existing_by_list.get(list_title, {}).items():
                record = task_state_record(state, tasklist_id, uid)
                if str(task.get("status") or "") == "completed" and not record:
                    continue
                if (tasklist_id, str(task.get("id") or "")) in claimed_task_ids:
                    continue
                if (list_title, uid) in blocked:
                    continue
                if (tasklist_id, uid) not in desired_keys and task.get("id"):
                    stale[(tasklist_id, uid)] = {
                        "task_id": str(task["id"]),
                        "list_title": list_title,
                        "task": task,
                        "record": record,
                    }

        for (tasklist_id, uid), candidate in stale.items():
            task_id = str(candidate["task_id"])
            list_title = str(candidate.get("list_title") or "")
            task = candidate.get("task") if isinstance(candidate.get("task"), dict) else None
            record = candidate.get("record") if isinstance(candidate.get("record"), dict) else None
            completed_item = completed_by_list.get(list_title, {}).get(uid)

            if config["tasks_complete_stale"] and completed_item:
                body, digest, reminder = completed_item
                title = str(body.get("title") or reminder.get("title") or uid)
                if dry_run:
                    print(f"DRY-RUN complete stale task from completed Apple Reminder: {title}")
                    continue

                if (
                    record
                    and record.get("apple_completed")
                    and record.get("digest") == digest
                    and task
                    and str(task.get("status") or "") == "completed"
                ):
                    continue

                if task and str(task.get("status") or "") == "completed":
                    patched = task
                else:
                    try:
                        patched = client.patch_task(tasklist_id, task_id, {"status": "completed"})
                        completed += 1
                    except GoogleApiError as exc:
                        if exc.status not in (404, 410):
                            raise
                        remove_task_state_record(state, tasklist_id, uid)
                        continue
                save_task_state(state, tasklist_id, uid, patched, digest, title, reminder, config, list_title)
                continue

            if dry_run:
                print(f"DRY-RUN delete stale task: {uid}")
                continue
            client.delete_task(tasklist_id, task_id)
            deleted += 1
            remove_task_state_record(state, tasklist_id, uid)

    if allow_deletes:
        for list_title, duplicates in duplicates_by_list.items():
            if not list_allows_apple_to_google(config, list_title):
                continue
            if not list_allows_delete_propagation(
                config,
                list_title,
                safety_allows_deletes=allow_deletes,
            ):
                continue
            tasklist_id = tasklists.get(list_title)
            if not tasklist_id:
                continue
            for task in duplicates:
                task_id = task.get("id")
                if not task_id:
                    continue
                if dry_run:
                    print(f"DRY-RUN delete duplicate task in [{list_title}]: {task.get('title', task_id)}")
                    continue
                client.delete_task(tasklist_id, str(task_id))
                duplicate_deleted += 1

    if config.get("verify_title_due_after_sync") and not dry_run:
        if bidir_conflicts:
            raise SystemExit(
                "Bidirectional conflicts left Apple Reminders and Google Tasks unsynced. "
                "Resolve the conflicts or use conflict_policy=newer_wins."
            )
        outbound_desired_by_list = {
            list_title: desired
            for list_title, desired in desired_by_list.items()
            if list_allows_apple_to_google(config, list_title) and list_title in tasklists
        }
        verify_google_tasks_title_due_consistency(
            client,
            outbound_desired_by_list,
            tasklists,
            blocked,
            recently_synced_task_ids=recently_synced_task_ids,
            retry_attempts=int(config.get("verify_title_due_retry_attempts") or 1),
            retry_delay_seconds=float(config.get("verify_title_due_retry_delay_seconds") or 0.0),
        )

    if not dry_run:
        write_json_atomic(state_path, state)

    print(
        "Sync summary: "
        f"inserted={inserted}, updated={updated}, unchanged={unchanged}, "
        f"completed={completed}, deleted={deleted}, duplicate_deleted={duplicate_deleted}, "
        f"skipped_invalid={skipped_invalid}, google_applied={google_applied}, "
        f"bidir_initialized={bidir_initialized}, bidir_conflicts={bidir_conflicts}"
    )
    if dry_run:
        print("Dry run only; no Google Tasks or Apple Reminders changes were made.")


def run_calendar_sync(config: dict[str, Any], dry_run: bool = False) -> None:
    calendar_id = str(config["calendar_id"])
    reminders, desired, skipped_invalid = build_desired_events(config)

    print(f"Apple Reminders exported: {len(reminders)}")
    print(f"Google Calendar events desired: {len(desired)}")

    state_path = expand_path(config["state_path"])
    state = load_state(state_path)
    account_binding = resolve_sync_account_binding(config, reminders)
    bind_or_validate_sync_state_accounts(state, account_binding)
    config["_expected_google_credential_binding"] = account_binding["google"]
    client = GoogleCalendarClient(config)
    existing_by_uid: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []

    existing_events = client.list_synced_events(calendar_id)
    existing_by_uid, duplicates = index_existing_events(existing_events)

    skip_stale_delete = config["delete_stale"] and not config["allow_empty_source_delete"] and len(desired) == 0
    allow_deletes = bool(config["delete_stale"] and not skip_stale_delete)
    bidirectional_plan = plan_google_changes_to_reminders(
        config,
        calendar_id,
        desired,
        existing_by_uid,
        state,
    )
    preflight_blocked = set(bidirectional_plan["blocked_uids"]) | set(bidirectional_plan["source_controlled"])
    planned_actions = list(bidirectional_plan["actions"])
    planned_actions.extend(
        plan_google_calendar_outbound_mutations(
            config,
            calendar_id,
            desired,
            existing_by_uid,
            duplicates,
            state,
            preflight_blocked,
            allow_deletes=allow_deletes,
        )
    )
    population = max(len(desired), managed_google_calendar_population(calendar_id, existing_by_uid, state))
    mutation_plan = build_mutation_plan(planned_actions, population)
    enforce_mutation_plan(config, mutation_plan, dry_run=dry_run)

    google_applied, bidir_initialized, bidir_conflicts, blocked_uids = apply_google_changes_to_reminders(
        config,
        calendar_id,
        desired,
        existing_by_uid,
        state,
        dry_run,
        planned=bidirectional_plan,
    )
    if google_applied and not dry_run:
        time.sleep(1)
        reminders, desired, skipped_invalid = build_desired_events(config)
        print(f"Apple Reminders re-exported after Google changes: {len(reminders)}")
        print(f"Google Calendar events desired: {len(desired)}")

    inserted = updated = unchanged = deleted = duplicate_deleted = 0

    for uid, (body, digest, reminder) in desired.items():
        if uid in blocked_uids:
            continue

        existing = existing_by_uid.get(uid)
        record = state_record(state, calendar_id, uid)
        event_id = existing.get("id") if existing else record.get("event_id") if record else None
        existing_digest = private_props(existing).get(DIGEST_KEY) if existing else record.get("digest") if record else None
        title = str(body.get("summary") or reminder.get("title") or uid)

        if dry_run:
            action = "update" if event_id and existing_digest != digest else "insert" if not event_id else "skip"
            print(f"DRY-RUN {action}: {title}")
            continue

        if existing and event_id and existing_digest == digest:
            unchanged += 1
            save_event_state(state, calendar_id, uid, existing, digest, title, reminder, config)
            continue

        if event_id:
            try:
                event = client.update_event(calendar_id, event_id, body)
                updated += 1
            except GoogleApiError as exc:
                if exc.status not in (404, 410):
                    raise
                event = client.insert_event(calendar_id, body)
                inserted += 1
        else:
            event = client.insert_event(calendar_id, body)
            inserted += 1

        save_event_state(state, calendar_id, uid, event, digest, title, reminder, config)

    desired_uids = set(desired)

    if skip_stale_delete:
        print("Skipping stale deletion because the Reminders source exported 0 items.")

    if config["delete_stale"] and not skip_stale_delete:
        stale: dict[str, str] = {}
        for key, record in list(state["events"].items()):
            if not isinstance(record, dict) or record.get("calendar_id") != calendar_id:
                continue
            uid = key.split(":", 1)[1] if ":" in key else ""
            if uid and uid not in desired_uids and record.get("event_id"):
                stale[uid] = str(record["event_id"])

        for uid, event in existing_by_uid.items():
            if uid not in desired_uids and event.get("id"):
                stale[uid] = str(event["id"])

        for uid, event_id in stale.items():
            if dry_run:
                print(f"DRY-RUN delete stale: {uid}")
                continue
            client.delete_event(calendar_id, event_id)
            remove_state_record(state, calendar_id, uid)
            deleted += 1

    if allow_deletes and duplicates:
        for event in duplicates:
            event_id = event.get("id")
            if not event_id:
                continue
            if dry_run:
                print(f"DRY-RUN delete duplicate: {event.get('summary', event_id)}")
                continue
            client.delete_event(calendar_id, str(event_id))
            duplicate_deleted += 1

    if not dry_run:
        write_json_atomic(state_path, state)

    print(
        "Sync summary: "
        f"inserted={inserted}, updated={updated}, unchanged={unchanged}, "
        f"deleted={deleted}, duplicate_deleted={duplicate_deleted}, skipped_invalid={skipped_invalid}, "
        f"google_applied={google_applied}, bidir_initialized={bidir_initialized}, "
        f"bidir_conflicts={bidir_conflicts}"
    )
    if dry_run:
        print("Dry run only; no Google Calendar or Apple Reminders changes were made.")


def run_sync(config: dict[str, Any], dry_run: bool = False) -> None:
    if config["target_service"] == "calendar":
        run_calendar_sync(config, dry_run=dry_run)
        return
    run_tasks_sync(config, dry_run=dry_run)


def utc_now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_sync_status(config: dict[str, Any], updates: dict[str, Any]) -> None:
    status_path = expand_path(config["status_path"])
    status: dict[str, Any] = {}
    if status_path.exists():
        try:
            status = read_json(status_path)
        except (OSError, json.JSONDecodeError):
            status = {}
    status.update(updates)
    status["updated_at"] = utc_now_text()
    write_json_atomic(status_path, status)


def read_sync_status(config: dict[str, Any]) -> dict[str, Any]:
    status_path = expand_path(config["status_path"])
    if not status_path.exists():
        return {}
    try:
        return read_json(status_path)
    except (OSError, json.JSONDecodeError):
        return {}


def parse_status_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def seconds_until_retry(last_attempt: dt.datetime | None, interval_seconds: int, now: dt.datetime | None = None) -> int:
    if not last_attempt:
        return 0
    now = now or dt.datetime.now(dt.timezone.utc)
    elapsed = (now - last_attempt).total_seconds()
    return max(0, int(interval_seconds - elapsed))


def auto_reauth_failure_count(status: dict[str, Any]) -> int:
    counts = []
    for key in ("auto_reauth_failure_count", "auto_reauth_timeout_count"):
        try:
            counts.append(max(0, int(status.get(key) or 0)))
        except (TypeError, ValueError):
            continue
    return max(counts, default=0)


def auto_reauth_failure_epoch_reset_updates() -> dict[str, Any]:
    return {
        "last_auto_reauth_failed_at": None,
        "last_auto_reauth_timeout_at": None,
        "last_auto_reauth_runtime_failure_at": None,
        "auto_reauth_failure_count": 0,
        "auto_reauth_timeout_count": 0,
    }


def auto_reauth_failure_backoff_seconds(config: dict[str, Any], status: dict[str, Any]) -> int:
    base_interval = max(300, int(config.get("auto_reauth_timeout_retry_interval_seconds") or 300))
    max_interval = max(base_interval, int(config.get("auto_reauth_min_interval_seconds") or 21600))
    failure_count = max(1, auto_reauth_failure_count(status))
    exponent = min(failure_count - 1, 16)
    return min(max_interval, base_interval * (2**exponent))


def auto_reauth_retry_wait_seconds(
    config: dict[str, Any],
    status: dict[str, Any],
    now: dt.datetime | None = None,
) -> int:
    last_runtime_failure = parse_status_time(status.get("last_auto_reauth_runtime_failure_at"))
    last_timeout = parse_status_time(status.get("last_auto_reauth_timeout_at"))
    last_failure = parse_status_time(status.get("last_auto_reauth_failed_at"))
    last_started = parse_status_time(status.get("last_auto_reauth_started_at"))
    failure_times = [value for value in (last_failure, last_runtime_failure, last_timeout) if value]
    latest_failure = max(failure_times) if failure_times else None
    if latest_failure and (not last_started or latest_failure >= last_started):
        interval = auto_reauth_failure_backoff_seconds(config, status)
        return seconds_until_retry(latest_failure, interval, now=now)

    interval = max(300, int(config.get("auto_reauth_min_interval_seconds") or 21600))
    return seconds_until_retry(last_started, interval, now=now)


def recent_auto_reauth(config: dict[str, Any], status: dict[str, Any]) -> bool:
    return auto_reauth_retry_wait_seconds(config, status) > 0


def truncate_notification_text(value: str, limit: int = 180) -> str:
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "..."


def send_macos_notification(
    config: dict[str, Any],
    title: str,
    message: str,
    status_key: str,
    min_interval_seconds: int,
) -> None:
    if not config.get("macos_notifications"):
        return
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return

    status = read_sync_status(config)
    wait_seconds = seconds_until_retry(
        parse_status_time(status.get(status_key)),
        max(0, min_interval_seconds),
    )
    if wait_seconds > 0:
        return

    script = """
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script, title, truncate_notification_text(message)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return

    if result.returncode == 0:
        write_sync_status(config, {status_key: utc_now_text()})


def notify_sync_ok(config: dict[str, Any]) -> None:
    send_macos_notification(
        config,
        "iCloud Reminders Sync",
        "Apple Reminders and Google Tasks are synced.",
        "last_success_notification_at",
        int(config.get("notify_success_min_interval_seconds") or 3600),
    )


def notify_sync_problem(config: dict[str, Any], message: str) -> None:
    send_macos_notification(
        config,
        "iCloud Reminders Sync Needs Attention",
        message,
        "last_failure_notification_at",
        int(config.get("notify_failure_min_interval_seconds") or 300),
    )


def local_reauth_can_satisfy_binding(config: dict[str, Any]) -> bool:
    """Whether a browser OAuth login could produce the credential the state expects.

    run_auth_flow only ever mints a local_oauth credential. When the sync state
    is bound to the gcloud ADC credential instead, that new token is a different
    identity and the binding guard has to reject it, so the flow opens a browser
    the owner did not ask for and leaves the loop exactly where it was.
    """
    try:
        state = load_state(expand_path(config["state_path"]))
    except Exception:
        return True
    bound = str((state.get("account_binding") or {}).get("google") or "")
    if not bound:
        return True
    for candidate in load_token_candidates(config):
        if str(candidate.get("_credential_source") or "") != "gcloud_adc":
            continue
        try:
            if google_credential_binding(candidate) == bound:
                return False
        except AuthenticationRequired:
            continue
    return True


def maybe_run_auto_reauth(config: dict[str, Any], reason: str) -> bool:
    if not config.get("auto_reauth_browser"):
        return False

    if not local_reauth_can_satisfy_binding(config):
        message = (
            "Sync state is bound to the gcloud ADC credential, so a browser OAuth login "
            "would mint a different identity that the account binding must reject. "
            "Restore the bound credential instead:\n"
            "  gcloud auth application-default login"
        )
        eprint(message)
        write_sync_status(
            config,
            {
                "state": "auth_required",
                "last_auto_reauth_skipped_at": utc_now_text(),
                "last_error": message,
            },
        )
        return False

    status = read_sync_status(config)
    wait_seconds = auto_reauth_retry_wait_seconds(config, status)
    if wait_seconds > 0:
        eprint(f"Automatic browser reauth was already attempted recently; retrying in {wait_seconds} seconds.")
        write_sync_status(
            config,
            {
                "state": "auth_required",
                "last_auto_reauth_skipped_at": utc_now_text(),
                "last_error": reason,
            },
        )
        return False

    started = utc_now_text()
    timeout_seconds = max(60, int(config.get("auto_reauth_timeout_seconds") or 300))
    print(
        f"Starting automatic Google OAuth browser login; waiting up to {timeout_seconds} seconds.",
        flush=True,
    )
    write_sync_status(
        config,
        {
            "state": "auth_prompt_open",
            "last_auto_reauth_started_at": started,
            "last_auto_reauth_runtime_failure_at": None,
            "last_error": reason,
        },
    )

    try:
        run_auth_flow(config)
    except OAuthCallbackTimeout as exc:
        latest_status = read_sync_status(config)
        timeout_count = int(latest_status.get("auto_reauth_timeout_count") or 0) + 1
        failure_count = auto_reauth_failure_count(latest_status) + 1
        write_sync_status(
            config,
            {
                "state": "auth_timeout",
                "last_auto_reauth_failed_at": utc_now_text(),
                "last_auto_reauth_timeout_at": utc_now_text(),
                "auto_reauth_timeout_count": timeout_count,
                "auto_reauth_failure_count": failure_count,
                "last_error": str(exc),
            },
        )
        notify_sync_problem(config, "Google login timed out. The sync agent will retry automatically.")
        eprint(f"Automatic browser reauth timed out: {exc}")
        return False
    except SystemExit as exc:
        latest_status = read_sync_status(config)
        failure_count = auto_reauth_failure_count(latest_status) + 1
        write_sync_status(
            config,
            {
                "state": "auth_required",
                "last_auto_reauth_failed_at": utc_now_text(),
                "auto_reauth_failure_count": failure_count,
                "last_error": str(exc),
            },
        )
        eprint(f"Automatic browser reauth failed: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - an auth helper failure must not stop the scheduler.
        latest_status = read_sync_status(config)
        failure_count = auto_reauth_failure_count(latest_status) + 1
        error_text = truncate_notification_text(str(exc) or exc.__class__.__name__)
        write_sync_status(
            config,
            {
                "state": "auth_required",
                "last_auto_reauth_failed_at": utc_now_text(),
                "last_auto_reauth_runtime_failure_at": utc_now_text(),
                "auto_reauth_failure_count": failure_count,
                "last_error": f"Automatic Google OAuth browser login failed: {error_text}",
            },
        )
        notify_sync_problem(config, "Google login could not start. The sync agent will retry automatically.")
        eprint(f"Automatic browser reauth failed unexpectedly: {error_text}")
        return False

    write_sync_status(
        config,
        {
            "state": "auth_refreshed",
            "last_auto_reauth_completed_at": utc_now_text(),
            **auto_reauth_failure_epoch_reset_updates(),
            "last_error": "",
        },
    )
    print("Automatic Google OAuth browser login completed.", flush=True)
    return True


def restored_consecutive_failures(status: dict[str, Any]) -> int:
    if str(status.get("state") or "") in {"ok", "dry_run_ok"}:
        return 0
    try:
        return max(0, int(status.get("consecutive_failures") or 0))
    except (TypeError, ValueError):
        return 0


def cmd_sync(args: argparse.Namespace) -> None:
    config = load_config(args)
    with sync_lock(config, wait=True) as acquired:
        if not acquired:
            print("Another sync is already running; skipping.")
            return
        started = utc_now_text()
        write_sync_status(
            config,
            {
                "state": "running",
                "last_start_at": started,
                "last_error": "",
                "mutation_plan": None,
            },
        )
        try:
            run_sync(config, dry_run=bool(args.dry_run))
        except MutationPlanApprovalRequired:
            notify_sync_problem(config, "Sync paused because a destructive mutation plan requires explicit approval.")
            raise
        except AccountBindingRequired as exc:
            write_sync_status(
                config,
                {
                    "state": "account_binding_required",
                    "last_end_at": utc_now_text(),
                    "last_error": str(exc),
                },
            )
            notify_sync_problem(config, "Sync paused because the Apple or Google account binding changed.")
            raise SystemExit(str(exc)) from exc
        except AuthenticationRequired as exc:
            write_sync_status(
                config,
                {
                    "state": "auth_required",
                    "last_end_at": utc_now_text(),
                    "last_error": str(exc),
                },
            )
            notify_sync_problem(config, "Google authentication is required. Complete the browser login to resume sync.")
            raise SystemExit(str(exc)) from exc
        except Exception as exc:
            write_sync_status(
                config,
                {
                    "state": "failed",
                    "last_end_at": utc_now_text(),
                    "last_error": str(exc),
                },
            )
            notify_sync_problem(config, f"Sync failed: {exc}")
            raise
        write_sync_status(
            config,
            {
                "state": "ok" if not args.dry_run else "dry_run_ok",
                "last_end_at": utc_now_text(),
                "last_success_at": utc_now_text(),
                **auto_reauth_failure_epoch_reset_updates(),
                "last_error": "",
            },
        )
        if not args.dry_run:
            notify_sync_ok(config)


def cmd_run_loop(args: argparse.Namespace) -> None:
    harden_runtime_log_modes()
    config = load_config(args)
    interval = int(config["sync_interval_seconds"])
    if interval < 60:
        raise SystemExit("sync_interval_seconds must be at least 60.")

    print(f"Starting sync loop every {interval} seconds. Press Ctrl-C to stop.", flush=True)
    consecutive_failures = restored_consecutive_failures(read_sync_status(config))
    blocked_fingerprint = ""
    blocked_streak = 0
    while True:
        started = utc_now_text()
        print(f"[{started}] sync start", flush=True)
        try:
            with sync_lock(config, wait=False) as acquired:
                if acquired:
                    write_sync_status(
                        config,
                        {
                            "state": "running",
                            "last_start_at": started,
                            "last_error": "",
                            "mutation_plan": None,
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    run_sync(config, dry_run=False)
                    consecutive_failures = 0
                    blocked_fingerprint, blocked_streak = "", 0
                    write_sync_status(
                        config,
                        {
                            "state": "ok",
                            "last_end_at": utc_now_text(),
                            "last_success_at": utc_now_text(),
                            **auto_reauth_failure_epoch_reset_updates(),
                            "last_error": "",
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    notify_sync_ok(config)
                else:
                    print("Another sync is already running; skipping this cycle.", flush=True)
        except KeyboardInterrupt:
            raise
        except MutationPlanApprovalRequired as exc:
            consecutive_failures += 1
            eprint(f"Sync loop blocked by mutation plan: {exc}")
            fingerprint = str((exc.summary or {}).get("fingerprint") or "")
            blocked_fingerprint, blocked_streak = next_blocked_plan_streak(
                blocked_fingerprint, blocked_streak, fingerprint
            )
            write_sync_status(
                config,
                {
                    "state": "blocked_mutation_plan",
                    "last_end_at": utc_now_text(),
                    "last_error": str(exc),
                    "mutation_plan": exc.summary,
                    "consecutive_failures": consecutive_failures,
                },
            )
            if should_auto_approve_blocked_plan(config, blocked_streak):
                print(
                    f"Auto-approving destructive mutation plan after {blocked_streak} identical consecutive plans.",
                    flush=True,
                )
                try:
                    with sync_lock(config, wait=False) as acquired:
                        if acquired:
                            config["_mutation_plan_approval"] = mutation_plan_approval_token(
                                {"fingerprint": blocked_fingerprint}
                            )
                            try:
                                run_sync(config, dry_run=False)
                            finally:
                                config["_mutation_plan_approval"] = ""
                            consecutive_failures = 0
                            blocked_fingerprint, blocked_streak = "", 0
                            write_sync_status(
                                config,
                                {
                                    "state": "ok",
                                    "last_end_at": utc_now_text(),
                                    "last_success_at": utc_now_text(),
                                    **auto_reauth_failure_epoch_reset_updates(),
                                    "last_error": "",
                                    "mutation_plan": None,
                                    "consecutive_failures": consecutive_failures,
                                },
                            )
                            notify_sync_ok(config)
                        else:
                            print("Another sync is already running; skipping auto-approval retry.", flush=True)
                except KeyboardInterrupt:
                    raise
                except Exception as retry_exc:  # noqa: BLE001 - loop must keep running
                    eprint(f"Auto-approved sync retry failed: {retry_exc}")
                    notify_sync_problem(
                        config,
                        "Bulk change auto-approval retry failed; will keep retrying on the next cycle.",
                    )
            else:
                notify_sync_problem(
                    config,
                    "Sync paused by a large destructive change plan; it will be applied automatically if it stays identical."
                    if int(config.get("auto_approve_destructive_loops", 0) or 0) > 0
                    else "Sync paused because a destructive mutation plan requires explicit approval.",
                )
        except AccountBindingRequired as exc:
            consecutive_failures += 1
            eprint(f"Sync loop account binding blocked: {exc}")
            write_sync_status(
                config,
                {
                    "state": "account_binding_required",
                    "last_end_at": utc_now_text(),
                    "last_error": str(exc),
                    "consecutive_failures": consecutive_failures,
                },
            )
            notify_sync_problem(config, "Sync paused because the Apple or Google account binding changed.")
        except AuthenticationRequired as exc:
            consecutive_failures += 1
            eprint(f"Sync loop auth required: {exc}")
            error_text = str(exc)
            write_sync_status(
                config,
                {
                    "state": "auth_required",
                    "last_end_at": utc_now_text(),
                    "last_error": error_text,
                    "consecutive_failures": consecutive_failures,
                },
            )
            notify_sync_problem(config, "Google authentication is required. Complete the browser login to resume sync.")
            if maybe_run_auto_reauth(config, error_text):
                print("Retrying sync after automatic Google OAuth login.", flush=True)
                try:
                    with sync_lock(config, wait=False) as acquired:
                        if acquired:
                            run_sync(config, dry_run=False)
                            consecutive_failures = 0
                            write_sync_status(
                                config,
                                {
                                    "state": "ok",
                                    "last_end_at": utc_now_text(),
                                    "last_success_at": utc_now_text(),
                                    **auto_reauth_failure_epoch_reset_updates(),
                                    "last_error": "",
                                    "consecutive_failures": consecutive_failures,
                                },
                            )
                            notify_sync_ok(config)
                        else:
                            print("Another sync is already running; skipping retry.", flush=True)
                except AuthenticationRequired as retry_exc:
                    consecutive_failures += 1
                    eprint(f"Sync retry still requires auth: {retry_exc}")
                    write_sync_status(
                        config,
                        {
                            "state": "auth_required",
                            "last_end_at": utc_now_text(),
                            "last_error": str(retry_exc),
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    notify_sync_problem(config, "Google authentication is still required after retry.")
                except MutationPlanApprovalRequired as retry_exc:
                    consecutive_failures += 1
                    eprint(f"Sync retry blocked by mutation plan: {retry_exc}")
                    write_sync_status(
                        config,
                        {
                            "state": "blocked_mutation_plan",
                            "last_end_at": utc_now_text(),
                            "last_error": str(retry_exc),
                            "mutation_plan": retry_exc.summary,
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    notify_sync_problem(
                        config,
                        "Sync paused because a destructive mutation plan requires explicit approval.",
                    )
                except AccountBindingRequired as retry_exc:
                    consecutive_failures += 1
                    eprint(f"Sync retry account binding blocked: {retry_exc}")
                    write_sync_status(
                        config,
                        {
                            "state": "account_binding_required",
                            "last_end_at": utc_now_text(),
                            "last_error": str(retry_exc),
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    notify_sync_problem(
                        config,
                        "Sync paused because the Apple or Google account binding changed.",
                    )
                except SystemExit as retry_exc:
                    consecutive_failures += 1
                    eprint(f"Sync retry error: {retry_exc}")
                    write_sync_status(
                        config,
                        {
                            "state": "failed",
                            "last_end_at": utc_now_text(),
                            "last_error": str(retry_exc),
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    notify_sync_problem(config, f"Sync retry failed: {retry_exc}")
                except Exception as retry_exc:  # noqa: BLE001 - keep the scheduler alive after retry failures.
                    consecutive_failures += 1
                    eprint(f"Sync retry error: {retry_exc}")
                    write_sync_status(
                        config,
                        {
                            "state": "failed",
                            "last_end_at": utc_now_text(),
                            "last_error": str(retry_exc),
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    notify_sync_problem(config, f"Sync retry failed: {retry_exc}")
        except SystemExit as exc:
            consecutive_failures += 1
            eprint(f"Sync loop error: {exc}")
            write_sync_status(
                config,
                {
                    "state": "failed",
                    "last_end_at": utc_now_text(),
                    "last_error": str(exc),
                    "consecutive_failures": consecutive_failures,
                },
            )
            notify_sync_problem(config, f"Sync failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - a scheduler should log and keep running.
            consecutive_failures += 1
            eprint(f"Sync loop error: {exc}")
            write_sync_status(
                config,
                {
                    "state": "failed",
                    "last_end_at": utc_now_text(),
                    "last_error": str(exc),
                    "consecutive_failures": consecutive_failures,
                },
            )
            notify_sync_problem(config, f"Sync failed: {exc}")
        print(f"[{utc_now_text()}] sync end", flush=True)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("Stopping sync loop.", flush=True)
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Apple Reminders to Google Tasks or Google Calendar.")
    parser.add_argument(
        "--config",
        default=str(default_config_dir() / "config.json"),
        help="Path to config JSON. Default: ~/.config/icloud-reminders-google-sync/config.json",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-config", help="Write a starter config file.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    init_parser.set_defaults(func=cmd_init_config)

    auth_parser = subparsers.add_parser("auth", help="Authorize Google API access.")
    auth_parser.add_argument("--credentials-path")
    auth_parser.set_defaults(func=cmd_auth)

    gcloud_login_parser = subparsers.add_parser(
        "gcloud-login",
        help="Authorize Google API access through gcloud Application Default Credentials.",
    )
    gcloud_login_parser.add_argument("--adc-credentials-path")
    gcloud_login_parser.set_defaults(func=cmd_gcloud_login)

    export_parser = subparsers.add_parser("export", help="Print Apple Reminders as JSON.")
    add_common_sync_options(export_parser)
    export_parser.set_defaults(func=cmd_export)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Inspect local prerequisites, LaunchAgent state, and the last sync result without changing runtime state.",
    )
    doctor_parser.add_argument(
        "--online",
        action="store_true",
        help="Also refresh Google auth and check the API; this can update the local OAuth token.",
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    manage_parser = subparsers.add_parser(
        "manage",
        help="Diagnose and safely recover the installed Google Tasks sync from one guided interface.",
    )
    manage_parser.add_argument(
        "action",
        nargs="?",
        choices=["menu", "status", "check", "reconnect", "restart"],
        default="menu",
        help="Management action. The default opens an interactive Korean menu.",
    )
    manage_parser.add_argument(
        "--yes",
        action="store_true",
        help="Accept management confirmations. Intended for controlled testing and recovery only.",
    )
    manage_parser.set_defaults(func=cmd_manage)

    sync_parser = subparsers.add_parser("sync", help="Sync Apple Reminders to Google Tasks or Google Calendar.")
    add_common_sync_options(sync_parser)
    sync_parser.add_argument("--credentials-path")
    sync_parser.add_argument("--adc-credentials-path")
    sync_parser.add_argument("--no-adc", action="store_true")
    sync_parser.add_argument("--token-path")
    sync_parser.add_argument("--state-path")
    sync_parser.add_argument("--status-path")
    sync_parser.add_argument("--auto-reauth-min-interval-seconds", type=int)
    sync_parser.add_argument("--auto-reauth-timeout-seconds", type=int)
    sync_parser.add_argument("--auto-reauth-timeout-retry-interval-seconds", type=int)
    sync_parser.add_argument("--notify-success-min-interval-seconds", type=int)
    sync_parser.add_argument("--notify-failure-min-interval-seconds", type=int)
    sync_parser.add_argument("--no-macos-notifications", action="store_true")
    sync_parser.add_argument("--no-verify-title-due-after-sync", action="store_true")
    sync_parser.add_argument("--verify-title-due-retry-attempts", type=int)
    sync_parser.add_argument("--verify-title-due-retry-delay-seconds", type=float)
    sync_parser.add_argument("--max-destructive-changes", type=int)
    sync_parser.add_argument("--max-destructive-ratio", type=float)
    sync_parser.add_argument("--destructive-approval-ttl-seconds", type=int)
    sync_parser.add_argument("--approve-mutation-plan")
    sync_parser.add_argument("--target-service", choices=["tasks", "calendar"])
    sync_parser.add_argument("--calendar-id")
    sync_parser.add_argument("--tasks-list-id")
    sync_parser.add_argument("--tasks-list-title")
    sync_parser.add_argument("--no-tasks-mirror-lists", action="store_true")
    sync_parser.add_argument("--no-tasks-mirror-empty-lists", action="store_true")
    sync_parser.add_argument("--no-tasks-create-missing-lists", action="store_true")
    sync_parser.add_argument("--no-tasks-complete-stale", action="store_true")
    sync_parser.add_argument("--tasks-import-unsynced", action="store_true", default=None)
    sync_parser.add_argument("--no-tasks-import-unsynced", action="store_true")
    sync_parser.add_argument("--tasks-sync-undated", action="store_true", default=None)
    sync_parser.add_argument("--no-tasks-sync-undated", action="store_true")
    sync_parser.add_argument("--reminders-apply-path")
    sync_parser.add_argument("--sync-interval-seconds", type=int)
    sync_parser.add_argument("--default-duration-minutes", type=int)
    sync_parser.add_argument("--prefix-list", action="store_true", default=None)
    sync_parser.add_argument("--no-delete-stale", action="store_true")
    sync_parser.add_argument("--allow-empty-source-delete", action="store_true", default=None)
    sync_parser.add_argument("--bidirectional", action="store_true", default=None)
    sync_parser.add_argument("--conflict-policy", choices=["skip", "newer_wins"])
    sync_parser.add_argument("--busy", action="store_true", help="Make synced events block calendar availability.")
    sync_parser.add_argument(
        "--google-popup-minutes",
        action="append",
        type=int,
        help="Add a Google popup reminder this many minutes before the event. May be repeated.",
    )
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.set_defaults(func=cmd_sync)

    loop_parser = subparsers.add_parser("run-loop", help="Run sync repeatedly in the foreground.")
    add_common_sync_options(loop_parser)
    loop_parser.add_argument("--credentials-path")
    loop_parser.add_argument("--adc-credentials-path")
    loop_parser.add_argument("--no-adc", action="store_true")
    loop_parser.add_argument("--token-path")
    loop_parser.add_argument("--state-path")
    loop_parser.add_argument("--status-path")
    loop_parser.add_argument("--auto-reauth-min-interval-seconds", type=int)
    loop_parser.add_argument("--auto-reauth-timeout-seconds", type=int)
    loop_parser.add_argument("--auto-reauth-timeout-retry-interval-seconds", type=int)
    loop_parser.add_argument("--notify-success-min-interval-seconds", type=int)
    loop_parser.add_argument("--notify-failure-min-interval-seconds", type=int)
    loop_parser.add_argument("--no-macos-notifications", action="store_true")
    loop_parser.add_argument("--no-verify-title-due-after-sync", action="store_true")
    loop_parser.add_argument("--verify-title-due-retry-attempts", type=int)
    loop_parser.add_argument("--verify-title-due-retry-delay-seconds", type=float)
    loop_parser.add_argument("--max-destructive-changes", type=int)
    loop_parser.add_argument("--max-destructive-ratio", type=float)
    loop_parser.add_argument("--destructive-approval-ttl-seconds", type=int)
    loop_parser.add_argument("--approve-mutation-plan")
    loop_parser.add_argument("--target-service", choices=["tasks", "calendar"])
    loop_parser.add_argument("--calendar-id")
    loop_parser.add_argument("--tasks-list-id")
    loop_parser.add_argument("--tasks-list-title")
    loop_parser.add_argument("--no-tasks-mirror-lists", action="store_true")
    loop_parser.add_argument("--no-tasks-mirror-empty-lists", action="store_true")
    loop_parser.add_argument("--no-tasks-create-missing-lists", action="store_true")
    loop_parser.add_argument("--no-tasks-complete-stale", action="store_true")
    loop_parser.add_argument("--tasks-import-unsynced", action="store_true", default=None)
    loop_parser.add_argument("--no-tasks-import-unsynced", action="store_true")
    loop_parser.add_argument("--tasks-sync-undated", action="store_true", default=None)
    loop_parser.add_argument("--no-tasks-sync-undated", action="store_true")
    loop_parser.add_argument("--reminders-apply-path")
    loop_parser.add_argument("--sync-interval-seconds", type=int)
    loop_parser.add_argument("--default-duration-minutes", type=int)
    loop_parser.add_argument("--prefix-list", action="store_true", default=None)
    loop_parser.add_argument("--no-delete-stale", action="store_true")
    loop_parser.add_argument("--allow-empty-source-delete", action="store_true", default=None)
    loop_parser.add_argument("--bidirectional", action="store_true", default=None)
    loop_parser.add_argument("--conflict-policy", choices=["skip", "newer_wins"])
    loop_parser.add_argument("--busy", action="store_true", help="Make synced events block calendar availability.")
    loop_parser.add_argument(
        "--google-popup-minutes",
        action="append",
        type=int,
        help="Add a Google popup reminder this many minutes before the event. May be repeated.",
    )
    loop_parser.set_defaults(func=cmd_run_loop)

    return parser


def add_common_sync_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reminders-exporter-path")
    parser.add_argument("--reminders-source", choices=["auto", "eventkit", "sqlite"])
    parser.add_argument("--reminders-sqlite-dir")
    parser.add_argument("--lookahead-days", type=int)
    parser.add_argument(
        "--include-lists",
        "--list",
        dest="include_lists",
        action="append",
        help="Limit to a Reminders list. May be repeated.",
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
