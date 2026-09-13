import unittest
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from unittest import mock

import icloud_reminders_google_sync as sync


class AccountBindingIsolationTests(unittest.TestCase):
    @staticmethod
    def binding(apple_token: str, google_token: str) -> dict[str, object]:
        return {
            "version": 1,
            "apple": sync.sha256_text(f"apple:{apple_token}"),
            "google": sync.sha256_text(f"google:{google_token}"),
        }

    def test_resolved_binding_hashes_apple_and_google_account_identifiers(self) -> None:
        config = sync.default_config()
        apple_canary = "TENANT_A_ONLY_APPLE_ACCOUNT"
        google_canary = "TENANT_A_ONLY_GOOGLE_REFRESH_TOKEN"

        with mock.patch.object(
            sync,
            "run_reminders_lists_export",
            return_value=[{"id": "list-a", "account_id": apple_canary}],
        ), mock.patch.object(
            sync,
            "load_token",
            return_value={"refresh_token": google_canary},
        ):
            binding = sync.resolve_sync_account_binding(config, [])

        serialized = json.dumps(binding, sort_keys=True)
        self.assertNotIn(apple_canary, serialized)
        self.assertNotIn(google_canary, serialized)
        self.assertEqual(binding["version"], 1)

    def test_account_switch_and_unbound_legacy_state_fail_closed(self) -> None:
        account_a = self.binding("TENANT_A_ONLY_APPLE", "TENANT_A_ONLY_GOOGLE")
        account_b = self.binding("TENANT_B_ONLY_APPLE", "TENANT_B_ONLY_GOOGLE")
        state: dict[str, object] = {"version": 1, "events": {}, "tasks": {}}
        sync.bind_or_validate_sync_state_accounts(state, account_a)
        before = json.loads(json.dumps(state))

        with self.assertRaises(sync.AccountBindingRequired):
            sync.bind_or_validate_sync_state_accounts(state, account_b)

        self.assertEqual(state, before)
        legacy = {
            "version": 1,
            "events": {},
            "tasks": {"TENANT_A_ONLY_TASK": {"task_id": "opaque-a"}},
        }
        with self.assertRaises(sync.AccountBindingRequired):
            sync.bind_or_validate_sync_state_accounts(legacy, account_b)
        self.assertNotIn("account_binding", legacy)

    def test_tasks_sync_rejects_account_b_before_google_query_or_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            state_path = base / "state.json"
            account_a = self.binding("TENANT_A_ONLY_APPLE", "TENANT_A_ONLY_GOOGLE")
            account_b = self.binding("TENANT_B_ONLY_APPLE", "TENANT_B_ONLY_GOOGLE")
            state = {
                "version": 1,
                "account_binding": account_a,
                "events": {},
                "tasks": {"TENANT_A_ONLY_TASK": {"task_id": "opaque-a"}},
            }
            sync.write_json_atomic(state_path, state)
            before = state_path.read_bytes()
            config = sync.default_config()
            config.update({
                "state_path": str(state_path),
                "status_path": str(base / "status.json"),
            })
            reminder_b = {
                "stable_id": "TENANT_B_ONLY_REMINDER",
                "title": "Tenant B synthetic reminder",
                "list_title": "Synthetic",
                "account_id": "TENANT_B_ONLY_APPLE",
            }

            with mock.patch.object(
                sync,
                "build_desired_tasks",
                return_value=([reminder_b], {"Synthetic": {}}, 0),
            ), mock.patch.object(
                sync,
                "resolve_sync_account_binding",
                return_value=account_b,
            ), mock.patch.object(
                sync,
                "GoogleTasksClient",
                side_effect=AssertionError("Google API must not be queried after an account switch"),
            ):
                with self.assertRaises(sync.AccountBindingRequired):
                    sync.run_tasks_sync(config)

            self.assertEqual(state_path.read_bytes(), before)

    def test_auto_reauth_is_skipped_when_state_is_bound_to_adc(self) -> None:
        # run_auth_flow only mints a local_oauth credential, so for ADC-bound
        # state it opens a browser and produces a token the binding guard must
        # reject. Running it can never converge, so it must not run.
        adc = {"refresh_token": "ADC_ONLY_REFRESH", "_credential_source": "gcloud_adc"}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "account_binding": {
                            "version": 1,
                            "apple": "apple-binding",
                            "google": sync.google_credential_binding(adc),
                        },
                        "events": {},
                        "tasks": {},
                    }
                ),
                encoding="utf-8",
            )
            config = sync.default_config()
            config["state_path"] = str(state_path)
            config["auto_reauth_browser"] = True

            with mock.patch.object(sync, "load_token_candidates", return_value=[adc]):
                self.assertFalse(sync.local_reauth_can_satisfy_binding(config))
                with mock.patch.object(
                    sync,
                    "run_auth_flow",
                    side_effect=AssertionError("a browser login must not be opened"),
                ), mock.patch.object(sync, "write_sync_status") as write_status:
                    self.assertFalse(sync.maybe_run_auto_reauth(config, "expired"))

            recorded = write_status.call_args.args[1]
            self.assertEqual(recorded["state"], "auth_required")
            self.assertIn("gcloud auth application-default login", recorded["last_error"])

    def test_auto_reauth_still_runs_when_state_is_bound_to_the_local_credential(self) -> None:
        local = {"refresh_token": "LOCAL_ONLY_REFRESH", "_credential_source": "local_oauth"}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "account_binding": {
                            "version": 1,
                            "apple": "apple-binding",
                            "google": sync.google_credential_binding(local),
                        },
                        "events": {},
                        "tasks": {},
                    }
                ),
                encoding="utf-8",
            )
            config = sync.default_config()
            config["state_path"] = str(state_path)

            with mock.patch.object(sync, "load_token_candidates", return_value=[local]):
                self.assertTrue(sync.local_reauth_can_satisfy_binding(config))

    def test_auto_reauth_runs_when_nothing_is_bound_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            config = sync.default_config()
            config["state_path"] = str(state_path)
            self.assertTrue(sync.local_reauth_can_satisfy_binding(config))

    def test_refresh_fallback_cannot_cross_the_bound_google_credential(self) -> None:
        token_a = {"refresh_token": "TENANT_A_ONLY_GOOGLE_REFRESH"}
        token_b = {"refresh_token": "TENANT_B_ONLY_GOOGLE_REFRESH"}
        config = sync.default_config()
        config["_expected_google_credential_binding"] = sync.google_credential_binding(token_a)
        invalid_grant = sync.OAuthTokenError(
            400,
            '{"error":"invalid_grant"}',
            {"error": "invalid_grant"},
        )

        with mock.patch.object(sync, "refresh_token", side_effect=invalid_grant) as refresh, mock.patch.object(
            sync, "load_fallback_token", return_value=token_b
        ):
            # The fallback must never be used, but it must also not become a
            # dead end: the caller needs the actionable reauthorization request.
            with self.assertRaises(sync.AuthenticationRequired) as caught:
                sync.refresh_token_with_fallback(config, token_a)

        self.assertIn("gcloud-login", str(caught.exception))
        # Still exactly one call, with token_a: the mismatched credential was
        # never sent to Google.
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(refresh.call_args.args[1], token_a)

    def test_client_rechecks_google_binding_before_using_a_replaced_token(self) -> None:
        token_a = {
            "refresh_token": "TENANT_A_ONLY_GOOGLE_REFRESH",
            "access_token": "tenant-a-access",
            "expires_at": 4_102_444_800,
        }
        token_b = {
            "refresh_token": "TENANT_B_ONLY_GOOGLE_REFRESH",
            "access_token": "tenant-b-access",
            "expires_at": 4_102_444_800,
        }
        config = sync.default_config()
        config["_expected_google_credential_binding"] = sync.google_credential_binding(token_a)

        with mock.patch.object(sync, "load_token", return_value=token_b):
            client = sync.GoogleTasksClient(config)
            with self.assertRaises(sync.AccountBindingRequired):
                client.access_token()


class GoogleTasksDueTests(unittest.TestCase):
    def test_reminders_apply_uses_stable_list_id_without_unsafe_title_fallback(self) -> None:
        source = (Path(__file__).resolve().parent / "RemindersApply.swift").read_text(encoding="utf-8")

        self.assertIn("var calendarsByIdentifier: [String: EKCalendar]", source)
        self.assertIn("targetCalendar = calendarsByIdentifier[listID]", source)
        self.assertIn('missingReason = "list_id_not_found"', source)
        self.assertIn("else if titleMatches.count == 1", source)
        self.assertIn('missingReason = titleMatches.isEmpty ? "list_title_not_found" : "ambiguous_list_title"', source)

    def test_setup_keeps_launch_agent_logs_private_and_bounded(self) -> None:
        setup_source = (Path(__file__).resolve().parent / "setup-new-mac.sh").read_text(encoding="utf-8")
        self.assertIn('ICLOUD_SYNC_LOG_MAX_BYTES:-5242880', setup_source)
        self.assertIn('chmod 600 "$log_path"', setup_source)
        self.assertIn('mv "$log_path" "${log_path}.1"', setup_source)
        self.assertIn('"list_policies": {}', setup_source)
        self.assertLess(setup_source.index("prepare_runtime_logs\nwrite_launch_agent"), setup_source.rindex("load_launch_agent"))
        # The LaunchAgent logs carry reminder and task titles, so they must not
        # be written into the world-writable, world-readable /tmp.
        self.assertIn('LOG_DIR="${HOME}/Library/Logs/icloud-reminders-google-sync"', setup_source)
        self.assertIn('STDOUT_LOG="${LOG_DIR}/sync.out.log"', setup_source)
        self.assertIn('STDERR_LOG="${LOG_DIR}/sync.err.log"', setup_source)
        self.assertNotIn('\nSTDOUT_LOG="/tmp/', setup_source)
        self.assertNotIn('\nSTDERR_LOG="/tmp/', setup_source)
        self.assertNotIn("/tmp/icloud-reminders-google-sync-lists", setup_source)
        self.assertIn('chmod 700 "$LOG_DIR"', setup_source)

    def test_setup_does_not_embed_a_maintainer_google_cloud_project(self) -> None:
        setup_source = (Path(__file__).resolve().parent / "setup-new-mac.sh").read_text(encoding="utf-8")
        self.assertIn('GCP_PROJECT="${GCP_PROJECT:-}"', setup_source)
        self.assertIn('if [ -n "$GCP_PROJECT" ]; then', setup_source)
        self.assertNotRegex(setup_source, r'GCP_PROJECT="\$\{GCP_PROJECT:-[^}]')

    def test_doctor_default_is_local_only_and_hides_stored_error_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            config = sync.default_config()
            config.update(
                {
                    "_config_path": str(base / "config.json"),
                    "credentials_path": str(base / "credentials.json"),
                    "adc_credentials_path": str(base / "adc.json"),
                    "token_path": str(base / "token.json"),
                    "status_path": str(base / "status.json"),
                    "reminders_exporter_path": str(base / "RemindersExport.swift"),
                    "reminders_apply_path": str(base / "RemindersApply.swift"),
                }
            )
            for name in ("config.json", "token.json", "RemindersExport.swift", "RemindersApply.swift"):
                (base / name).write_text("{}", encoding="utf-8")
            (base / "status.json").write_text(
                json.dumps(
                    {
                        "state": "failed",
                        "updated_at": "2026-07-14T09:00:00+00:00",
                        "last_success_at": "2026-07-14T08:55:00+00:00",
                        "consecutive_failures": 2,
                        "last_error": f'access_token=super-secret local_path={base / "private.json"}',
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with mock.patch.object(sync, "load_config", return_value=config), mock.patch.object(
                sync, "launch_agent_loaded", return_value=False
            ), mock.patch.object(sync, "load_token", side_effect=AssertionError("doctor must stay offline")), contextlib.redirect_stdout(output):
                sync.cmd_doctor(mock.Mock(online=False))

            visible = output.getvalue()
            self.assertIn("Online Google checks: skipped (local read-only mode)", visible)
            self.assertIn("Last sync error: recorded; details hidden", visible)
            self.assertIn("Consecutive failures: 2", visible)
            self.assertIn("Next step:", visible)
            self.assertNotIn("super-secret", visible)
            self.assertNotIn(tmp_name, visible)

    def test_doctor_online_check_redacts_google_response_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            config = sync.default_config()
            config.update(
                {
                    "_config_path": str(base / "config.json"),
                    "credentials_path": str(base / "credentials.json"),
                    "adc_credentials_path": str(base / "adc.json"),
                    "token_path": str(base / "token.json"),
                    "status_path": str(base / "status.json"),
                    "reminders_exporter_path": str(base / "RemindersExport.swift"),
                    "reminders_apply_path": str(base / "RemindersApply.swift"),
                }
            )
            for name in ("config.json", "token.json", "RemindersExport.swift", "RemindersApply.swift"):
                (base / name).write_text("{}", encoding="utf-8")

            class FailingTasksClient:
                def __init__(self, _config: dict[str, object]) -> None:
                    self.token: dict[str, object] = {}

                @staticmethod
                def list_tasklists() -> list[dict[str, object]]:
                    raise sync.GoogleApiError(
                        403,
                        '{"error":{"message":"access_token=super-secret","local_path":"/Users/private"}}',
                    )

            output = io.StringIO()
            with mock.patch.object(sync, "load_config", return_value=config), mock.patch.object(
                sync, "launch_agent_loaded", return_value=True
            ), mock.patch.object(sync, "load_token", return_value={"refresh_token": "hidden"}), mock.patch.object(
                sync, "refresh_token_with_fallback", return_value={"access_token": "hidden"}
            ), mock.patch.object(sync, "GoogleTasksClient", FailingTasksClient), contextlib.redirect_stdout(output):
                sync.cmd_doctor(mock.Mock(online=True))

            visible = output.getvalue()
            self.assertIn("Google API request failed (HTTP 403)", visible)
            self.assertIn("Reauthorize Google access", visible)
            self.assertNotIn("super-secret", visible)
            self.assertNotIn("/Users/private", visible)

    def test_external_api_exception_strings_do_not_include_raw_bodies(self) -> None:
        google_error = sync.GoogleApiError(500, '{"access_token":"super-secret"}')
        oauth_error = sync.OAuthTokenError(
            400,
            '{"client_secret":"super-secret"}',
            {"error": "invalid_grant", "error_description": "/Users/private/token.json"},
        )

        self.assertEqual(
            str(google_error),
            "Google API request failed (HTTP 500). Google is temporarily unavailable; retry later.",
        )
        self.assertEqual(str(oauth_error), "Google OAuth token request failed (HTTP 400, code=invalid_grant).")
        self.assertNotIn("super-secret", str(google_error))
        self.assertNotIn("/Users/private", str(oauth_error))

    def test_doctor_gives_a_safe_next_step_after_a_successful_dry_run(self) -> None:
        next_step = sync.doctor_next_step(
            config_exists=True,
            swift_ready=True,
            helpers_ready=True,
            runtime_ready=True,
            auth_material_ready=True,
            launch_agent_installed=True,
            agent_loaded=True,
            status_result="available",
            status_state="dry_run_ok",
        )

        self.assertIn("--no-delete-stale", next_step)
        self.assertNotIn("approve", next_step.lower())

    def test_management_summary_prioritizes_account_binding_over_restart(self) -> None:
        condition, headline, action = sync.management_condition(
            {
                "config_exists": True,
                "runtime_ready": True,
                "running_from_stable_runtime": True,
                "helpers_ready": True,
                "status_state": "account_binding_required",
                "auth_material_ready": True,
                "agent_loaded": False,
                "status_result": "available",
            }
        )

        self.assertEqual(condition, "account_binding_required")
        self.assertIn("안전 정지", headline)
        self.assertIn("상태를 재구축", action)

    def test_google_account_identity_is_display_only_and_uses_openid_scopes(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"email": "owner@example.invalid", "sub": "PRIVATE-STABLE-GOOGLE-SUBJECT"}
        ).encode("utf-8")

        with mock.patch.object(sync.urllib.request, "urlopen", return_value=response):
            identity = sync.fetch_google_account_identity("PRIVATE-ACCESS-TOKEN")

        self.assertEqual(identity["account_email"], "owner@example.invalid")
        self.assertEqual(len(identity["account_fingerprint"]), 12)
        self.assertNotIn("PRIVATE-STABLE-GOOGLE-SUBJECT", json.dumps(identity))
        self.assertIn("openid", sync.LOCAL_OAUTH_SCOPES.split())
        self.assertIn("email", sync.LOCAL_OAUTH_SCOPES.split())

    def test_management_backup_is_private_and_excludes_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            config = sync.default_config()
            config.update(
                {
                    "_config_path": str(base / "config.json"),
                    "credentials_path": str(base / "credentials.json"),
                    "adc_credentials_path": str(base / "adc.json"),
                    "token_path": str(base / "token.json"),
                    "state_path": str(base / "state.json"),
                    "status_path": str(base / "status.json"),
                }
            )
            (base / "config.json").write_text("{}", encoding="utf-8")
            (base / "credentials.json").write_text("oauth-client", encoding="utf-8")
            (base / "state.json").write_text('{"tasks":{}}', encoding="utf-8")
            (base / "token-source.json").write_text("secret-token", encoding="utf-8")
            (base / "token.json").symlink_to(base / "token-source.json")

            backup_dir = sync.backup_management_files(config, "test")

            self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((backup_dir / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((backup_dir / "credentials.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((backup_dir / "state.json").stat().st_mode & 0o777, 0o600)
            self.assertFalse((backup_dir / "token.json").exists())

    def test_management_reconnect_rebuilds_binding_only_after_backup_and_safe_dry_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            config = sync.default_config()
            config.update(
                {
                    "_config_path": str(base / "config.json"),
                    "credentials_path": str(base / "credentials.json"),
                    "adc_credentials_path": str(base / "adc.json"),
                    "token_path": str(base / "token.json"),
                    "state_path": str(base / "state.json"),
                    "status_path": str(base / "status.json"),
                }
            )
            for name in ("config.json", "credentials.json", "adc.json", "token.json", "status.json"):
                (base / name).write_text("{}", encoding="utf-8")
            old_state = '{"tasks":{"old":{"task_id":"opaque"}}}'
            (base / "state.json").write_text(old_state, encoding="utf-8")
            snapshot = {
                "config_exists": True,
                "runtime_ready": True,
                "running_from_stable_runtime": True,
                "helpers_ready": True,
                "oauth_client_ready": True,
                "launch_agent_installed": True,
                "agent_loaded": True,
            }
            calls: list[bool] = []

            def safe_sync(_config: dict[str, object], *, dry_run: bool) -> None:
                calls.append(dry_run)
                if len(calls) == 1:
                    raise sync.AccountBindingRequired("changed")
                if not dry_run:
                    Path(config["state_path"]).write_text('{"tasks":{}}', encoding="utf-8")

            with mock.patch.object(sync, "print_management_summary", return_value=snapshot), mock.patch.object(
                sync, "management_confirm", return_value=True
            ), mock.patch.object(sync, "stop_management_agent", return_value=True), mock.patch.object(
                sync, "start_management_agent"
            ) as start_agent, mock.patch.object(
                sync,
                "check_google_connection",
                return_value={
                    "state": "ok",
                    "tasklist_count": 2,
                    "account_email": "intended@example.invalid",
                    "message": "ok",
                },
            ), mock.patch.object(sync, "authorize_google_for_management") as authorize, mock.patch.object(
                sync, "reminders_account_titles", return_value=["iCloud"]
            ), mock.patch.object(sync, "run_management_safe_sync", side_effect=safe_sync):
                sync.management_reconnect(config, mock.Mock(yes=True))

            self.assertEqual(calls, [True, True, False])
            authorize.assert_not_called()
            start_agent.assert_called_once_with(snapshot)
            backup_dirs = list((base / "backups").iterdir())
            self.assertEqual(len(backup_dirs), 1)
            archived = backup_dirs[0] / "state.active-before-rebuild.json"
            self.assertEqual(archived.read_text(encoding="utf-8"), old_state)
            self.assertEqual((base / "state.json").read_text(encoding="utf-8"), '{"tasks":{}}')

    def test_management_reconnect_refuses_a_disposable_checkout(self) -> None:
        snapshot = {
            "config_exists": True,
            "runtime_ready": True,
            "running_from_stable_runtime": False,
            "helpers_ready": True,
            "oauth_client_ready": True,
            "launch_agent_installed": True,
        }
        with mock.patch.object(sync, "print_management_summary", return_value=snapshot), mock.patch.object(
            sync, "backup_management_files", side_effect=AssertionError("must fail before backup or mutation")
        ):
            with self.assertRaises(sync.ManagementActionError):
                sync.management_reconnect(sync.default_config(), mock.Mock(yes=True))

    def test_release_installs_the_management_launcher_from_the_stable_runtime(self) -> None:
        root = Path(__file__).resolve().parent
        setup_source = (root / "setup-new-mac.sh").read_text(encoding="utf-8")
        bundle_source = (root / "make-migration-bundle.sh").read_text(encoding="utf-8")
        launcher_source = (root / "google-tasks-manager.command").read_text(encoding="utf-8")

        self.assertIn('SOURCE_MANAGER="${SCRIPT_DIR}/google-tasks-manager.command"', setup_source)
        self.assertIn('MANAGER_LAUNCHER="${HOME}/Applications/Google Tasks 동기화 관리.command"', setup_source)
        self.assertIn('ln -sfn "$MANAGER" "$MANAGER_LAUNCHER"', setup_source)
        self.assertIn('copy_required "google-tasks-manager.command"', bundle_source)
        self.assertIn('LAUNCHER_SOURCE="${BASH_SOURCE[0]}"', launcher_source)
        self.assertIn('RUNTIME_CURRENT="$(cd "$(dirname "$LAUNCHER_SOURCE")" && pwd -P)"', launcher_source)
        self.assertIn('manage "$@"', launcher_source)

    def test_build_task_sets_due_from_all_day_reminder(self) -> None:
        reminder = {
            "stable_id": "reminder-1",
            "title": "Pay bill",
            "notes": "",
            "list_title": "Personal",
            "list_id": "list-1",
            "account_id": "icloud",
            "due_date": "2026-05-08",
            "due_at": "2026-05-08T10:00:00Z",
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }

        _uid, body, _digest = sync.build_task(reminder, sync.default_config())

        self.assertEqual(body["due"], "2026-05-08T00:00:00.000Z")

    def test_build_task_sets_due_from_timed_reminder(self) -> None:
        reminder = {
            "stable_id": "reminder-2",
            "title": "Call bank",
            "notes": "",
            "list_title": "Personal",
            "list_id": "list-1",
            "account_id": "icloud",
            "due_date": None,
            "due_at": "2026-05-08T15:30:00+09:00",
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }

        _uid, body, _digest = sync.build_task(reminder, sync.default_config())

        self.assertEqual(body["due"], "2026-05-08T00:00:00.000Z")

    def test_existing_task_with_matching_digest_but_missing_due_needs_patch(self) -> None:
        config = sync.default_config()
        reminder = {
            "stable_id": "reminder-3",
            "title": "Renew passport",
            "notes": "Bring photo",
            "list_title": "Personal",
            "list_id": "list-1",
            "account_id": "icloud",
            "due_date": "2026-05-09",
            "due_at": None,
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }
        uid, body, digest = sync.build_task(reminder, config)
        existing = {
            "id": "task-1",
            "title": body["title"],
            "notes": sync.reminder_tasks_notes(reminder, uid, digest),
            "status": "needsAction",
        }

        self.assertTrue(sync.task_has_current_source_digest(existing, digest))
        self.assertTrue(sync.task_only_due_differs(existing, body, reminder, config))
        self.assertFalse(sync.task_matches_desired(existing, body, reminder, config))

    def test_existing_task_with_matching_due_is_desired(self) -> None:
        config = sync.default_config()
        reminder = {
            "stable_id": "reminder-4",
            "title": "Submit report",
            "notes": "",
            "list_title": "Work",
            "list_id": "list-2",
            "account_id": "icloud",
            "due_date": "2026-05-10",
            "due_at": None,
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }
        uid, body, digest = sync.build_task(reminder, config)
        existing = {
            "id": "task-2",
            "title": body["title"],
            "notes": sync.reminder_tasks_notes(reminder, uid, digest),
            "status": "needsAction",
            "due": body["due"],
        }

        self.assertFalse(sync.task_only_due_differs(existing, body, reminder, config))
        self.assertTrue(sync.task_matches_desired(existing, body, reminder, config))

    def test_title_due_consistency_verifies_synced_task(self) -> None:
        reminder = {
            "stable_id": "reminder-5",
            "title": "File tax form",
            "notes": "",
            "list_title": "Personal",
            "list_id": "list-1",
            "account_id": "icloud",
            "due_date": "2026-05-11",
            "due_at": None,
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }
        uid, body, digest = sync.build_task(reminder, sync.default_config())
        task = dict(body)
        task["id"] = "task-5"

        class FakeClient:
            def list_tasks(self, _tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                assert show_deleted
                return [task]

        verified = sync.verify_google_tasks_title_due_consistency(
            FakeClient(),  # type: ignore[arg-type]
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": "tasklist-1"},
            set(),
        )

        self.assertEqual(verified, 1)

    def test_title_due_consistency_rejects_due_mismatch(self) -> None:
        reminder = {
            "stable_id": "reminder-6",
            "title": "Renew insurance",
            "notes": "",
            "list_title": "Personal",
            "list_id": "list-1",
            "account_id": "icloud",
            "due_date": "2026-05-12",
            "due_at": None,
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }
        uid, body, digest = sync.build_task(reminder, sync.default_config())
        task = dict(body)
        task["id"] = "task-6"
        task["due"] = "2026-05-13T00:00:00.000Z"

        class FakeClient:
            def list_tasks(self, _tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                return [task]

        with self.assertRaises(SystemExit):
            sync.verify_google_tasks_title_due_consistency(
                FakeClient(),  # type: ignore[arg-type]
                {"Personal": {uid: (body, digest, reminder)}},
                {"Personal": "tasklist-1"},
                set(),
            )

    def test_title_due_consistency_fetches_recent_task_when_list_is_stale(self) -> None:
        reminder = {
            "stable_id": "reminder-7",
            "title": "Settle watch payment",
            "notes": "",
            "list_title": "Personal",
            "list_id": "list-1",
            "account_id": "icloud",
            "due_date": "2026-05-13",
            "due_at": None,
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }
        uid, body, digest = sync.build_task(reminder, sync.default_config())
        task = dict(body)
        task["id"] = "task-7"

        class FakeClient:
            def list_tasks(self, _tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                assert show_deleted
                return []

            def get_task(self, tasklist_id: str, task_id: str) -> dict[str, object]:
                assert tasklist_id == "tasklist-1"
                assert task_id == "task-7"
                return task

        verified = sync.verify_google_tasks_title_due_consistency(
            FakeClient(),  # type: ignore[arg-type]
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": "tasklist-1"},
            set(),
            recently_synced_task_ids={("tasklist-1", uid): "task-7"},
        )

        self.assertEqual(verified, 1)

    def test_title_due_consistency_retries_stale_list(self) -> None:
        reminder = {
            "stable_id": "reminder-8",
            "title": "Book appointment",
            "notes": "",
            "list_title": "Personal",
            "list_id": "list-1",
            "account_id": "icloud",
            "due_date": "2026-05-14",
            "due_at": None,
            "modified_at": "2026-05-07T12:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }
        uid, body, digest = sync.build_task(reminder, sync.default_config())
        task = dict(body)
        task["id"] = "task-8"

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def list_tasks(self, _tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                assert show_deleted
                self.calls += 1
                return [] if self.calls == 1 else [task]

        client = FakeClient()
        verified = sync.verify_google_tasks_title_due_consistency(
            client,  # type: ignore[arg-type]
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": "tasklist-1"},
            set(),
            retry_attempts=2,
            retry_delay_seconds=0,
        )

        self.assertEqual(verified, 1)
        self.assertEqual(client.calls, 2)

    def test_task_fallback_does_not_reuse_other_current_uid(self) -> None:
        task = {
            "id": "task-9",
            "notes": "Synced from Apple Reminders.\nSource UID: uid-1\nSource Digest: digest-1",
        }
        desired = {
            "uid-1": ({}, "digest-1", {}),
            "uid-2": ({}, "digest-2", {}),
        }

        self.assertFalse(
            sync.can_reuse_task_fallback(
                task,
                "uid-2",
                desired,
                "tasklist-1",
                set(),
            )
        )

    def test_task_fallback_can_reuse_retired_uid(self) -> None:
        task = {
            "id": "task-10",
            "notes": "Synced from Apple Reminders.\nSource UID: retired-uid\nSource Digest: digest-old",
        }
        desired = {
            "uid-10": ({}, "digest-10", {}),
        }

        self.assertTrue(
            sync.can_reuse_task_fallback(
                task,
                "uid-10",
                desired,
                "tasklist-1",
                set(),
            )
        )

    def test_task_state_record_does_not_reuse_other_current_uid(self) -> None:
        task = {
            "id": "task-11",
            "notes": "Synced from Apple Reminders.\nSource UID: uid-11\nSource Digest: digest-11",
        }
        desired = {
            "uid-11": ({}, "digest-11", {}),
            "uid-12": ({}, "digest-12", {}),
        }
        record = {"task_id": "task-11"}

        self.assertFalse(
            sync.can_use_task_state_record(
                record,
                "uid-12",
                desired,
                "tasklist-1",
                {"task-11": task},
                set(),
            )
        )

    def test_google_task_newer_than_reminder_wins_conflict(self) -> None:
        task = {"updated": "2026-05-08T10:00:00Z"}
        reminder = {"modified_at": "2026-05-08T09:59:00Z"}

        self.assertTrue(sync.google_task_is_newer_than_reminder(task, reminder))

    def test_reauth_timeout_uses_short_retry_interval(self) -> None:
        config = sync.default_config()
        config["auto_reauth_min_interval_seconds"] = 21600
        config["auto_reauth_timeout_retry_interval_seconds"] = 300
        status = {
            "last_auto_reauth_started_at": "2026-05-08T10:00:00+00:00",
            "last_auto_reauth_timeout_at": "2026-05-08T10:05:00+00:00",
        }
        now = dt.datetime(2026, 5, 8, 10, 7, tzinfo=dt.timezone.utc)

        wait_seconds = sync.auto_reauth_retry_wait_seconds(config, status, now=now)

        self.assertEqual(wait_seconds, 180)
        normal_failure_wait = sync.auto_reauth_retry_wait_seconds(
            config,
            {"last_auto_reauth_started_at": "2026-05-08T10:00:00+00:00"},
            now=now,
        )
        self.assertEqual(normal_failure_wait, 21180)

    def test_unexpected_auto_reauth_failure_is_recorded_without_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = sync.default_config()
            config.update(
                {
                    "auto_reauth_browser": True,
                    "macos_notifications": False,
                    "status_path": str(Path(tmp_name) / "status.json"),
                    # Keep the test hermetic: without these it reads the real
                    # state file and ADC credentials from the developer's home.
                    "state_path": str(Path(tmp_name) / "state.json"),
                    "adc_credentials_path": str(Path(tmp_name) / "adc.json"),
                }
            )

            with mock.patch.object(sync, "run_auth_flow", side_effect=OSError("loopback unavailable")):
                refreshed = sync.maybe_run_auto_reauth(config, "expired token")

            status = sync.read_sync_status(config)
            self.assertFalse(refreshed)
            self.assertEqual(status["state"], "auth_required")
            self.assertEqual(status["auto_reauth_failure_count"], 1)
            self.assertIn("loopback unavailable", status["last_error"])
            runtime_failure_at = sync.parse_status_time(status["last_auto_reauth_runtime_failure_at"])
            self.assertIsNotNone(runtime_failure_at)
            self.assertEqual(
                sync.auto_reauth_retry_wait_seconds(
                    config,
                    status,
                    now=runtime_failure_at + dt.timedelta(seconds=1),
                ),
                299,
            )

            with mock.patch.object(sync, "run_auth_flow") as automatic_auth:
                refreshed = sync.maybe_run_auto_reauth(config, "expired token")

            self.assertFalse(refreshed)
            automatic_auth.assert_not_called()
            self.assertEqual(sync.read_sync_status(config)["auto_reauth_failure_count"], 1)

            Path(config["status_path"]).unlink()
            with mock.patch.object(sync, "run_auth_flow", side_effect=SystemExit("consent denied")):
                refreshed = sync.maybe_run_auto_reauth(config, "expired token")

            normal_failure_status = sync.read_sync_status(config)
            self.assertFalse(refreshed)
            self.assertIsNone(normal_failure_status["last_auto_reauth_runtime_failure_at"])
            normal_failed_at = sync.parse_status_time(normal_failure_status["last_auto_reauth_failed_at"])
            self.assertIsNotNone(normal_failed_at)
            self.assertEqual(normal_failure_status["auto_reauth_failure_count"], 1)
            self.assertEqual(
                sync.auto_reauth_retry_wait_seconds(
                    config,
                    normal_failure_status,
                    now=normal_failed_at + dt.timedelta(seconds=1),
                ),
                299,
            )

    def test_automatic_reauth_failure_backoff_is_exponential_and_bounded(self) -> None:
        config = sync.default_config()
        config["auto_reauth_timeout_retry_interval_seconds"] = 300
        config["auto_reauth_min_interval_seconds"] = 1200
        failed_at = dt.datetime(2026, 5, 8, 10, 0, tzinfo=dt.timezone.utc)
        timestamp = failed_at.isoformat()

        waits = []
        for failure_count in (1, 2, 3, 8):
            waits.append(
                sync.auto_reauth_retry_wait_seconds(
                    config,
                    {
                        "last_auto_reauth_started_at": timestamp,
                        "last_auto_reauth_failed_at": timestamp,
                        "auto_reauth_failure_count": failure_count,
                    },
                    now=failed_at + dt.timedelta(seconds=1),
                )
            )

        self.assertEqual(waits, [299, 599, 1199, 1199])

    def test_interactive_auth_does_not_consult_automatic_retry_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = sync.default_config()
            config.update(
                {
                    "auto_reauth_browser": True,
                    "macos_notifications": False,
                    "status_path": str(Path(tmp_name) / "status.json"),
                    # Keep the test hermetic: without these it reads the real
                    # state file and ADC credentials from the developer's home.
                    "state_path": str(Path(tmp_name) / "state.json"),
                    "adc_credentials_path": str(Path(tmp_name) / "adc.json"),
                }
            )
            sync.write_sync_status(
                config,
                {
                    "last_auto_reauth_started_at": "2020-01-01T00:00:00+00:00",
                    "last_auto_reauth_failed_at": "2020-01-01T00:05:00+00:00",
                    "last_auto_reauth_timeout_at": "2020-01-01T00:05:00+00:00",
                    "last_auto_reauth_runtime_failure_at": "2020-01-01T00:05:00+00:00",
                    "auto_reauth_failure_count": 4,
                    "auto_reauth_timeout_count": 3,
                },
            )
            with mock.patch.object(sync, "load_config", return_value=config), mock.patch.object(
                sync, "auto_reauth_retry_wait_seconds", side_effect=AssertionError("interactive auth must not be throttled")
            ), mock.patch.object(sync, "run_auth_flow") as auth_flow:
                sync.cmd_auth(mock.Mock())

            auth_flow.assert_called_once_with(config)
            status = sync.read_sync_status(config)
            self.assertEqual(status["auto_reauth_failure_count"], 0)
            self.assertEqual(status["auto_reauth_timeout_count"], 0)
            self.assertIsNone(status["last_auto_reauth_failed_at"])
            self.assertIsNone(status["last_auto_reauth_timeout_at"])
            self.assertIsNone(status["last_auto_reauth_runtime_failure_at"])

            with mock.patch.object(sync, "run_auth_flow", side_effect=OSError("loopback unavailable")):
                self.assertFalse(sync.maybe_run_auto_reauth(config, "expired token"))

            self.assertEqual(sync.read_sync_status(config)["auto_reauth_failure_count"], 1)

    def test_successful_sync_clears_automatic_reauth_failure_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = sync.default_config()
            config.update(
                {
                    "macos_notifications": False,
                    "status_path": str(Path(tmp_name) / "status.json"),
                    "lock_path": str(Path(tmp_name) / "sync.lock"),
                }
            )
            sync.write_sync_status(
                config,
                {
                    "last_auto_reauth_failed_at": "2026-05-08T10:05:00+00:00",
                    "last_auto_reauth_timeout_at": "2026-05-08T10:05:00+00:00",
                    "last_auto_reauth_runtime_failure_at": "2026-05-08T10:05:00+00:00",
                    "auto_reauth_failure_count": 5,
                    "auto_reauth_timeout_count": 2,
                },
            )

            args = mock.Mock(dry_run=False)
            with mock.patch.object(sync, "load_config", return_value=config), mock.patch.object(
                sync, "run_sync"
            ) as run_sync, mock.patch.object(sync, "notify_sync_ok"):
                sync.cmd_sync(args)

            run_sync.assert_called_once_with(config, dry_run=False)
            status = sync.read_sync_status(config)
            self.assertEqual(status["state"], "ok")
            self.assertEqual(status["auto_reauth_failure_count"], 0)
            self.assertEqual(status["auto_reauth_timeout_count"], 0)
            self.assertIsNone(status["last_auto_reauth_failed_at"])
            self.assertIsNone(status["last_auto_reauth_timeout_at"])
            self.assertIsNone(status["last_auto_reauth_runtime_failure_at"])

    def test_failure_count_survives_scheduler_restart_until_success(self) -> None:
        self.assertEqual(
            sync.restored_consecutive_failures(
                {"state": "auth_required", "consecutive_failures": 4}
            ),
            4,
        )
        self.assertEqual(
            sync.restored_consecutive_failures(
                {"state": "ok", "consecutive_failures": 4}
            ),
            0,
        )

    def test_busy_loop_does_not_overwrite_active_sync_status(self) -> None:
        config = sync.default_config()
        config["sync_interval_seconds"] = 60

        @contextlib.contextmanager
        def busy_lock(_config, wait):
            self.assertFalse(wait)
            yield False

        with mock.patch.object(sync, "load_config", return_value=config), mock.patch.object(
            sync, "sync_lock", busy_lock
        ), mock.patch.object(sync, "write_sync_status") as write_status, mock.patch.object(
            sync.time, "sleep", side_effect=KeyboardInterrupt
        ):
            sync.cmd_run_loop(mock.Mock())

        write_status.assert_not_called()


class ListPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account_binding_patch = mock.patch.object(
            sync,
            "resolve_sync_account_binding",
            return_value=AccountBindingIsolationTests.binding(
                "TENANT_A_ONLY_APPLE_TEST_ACCOUNT",
                "TENANT_A_ONLY_GOOGLE_TEST_ACCOUNT",
            ),
        )
        self.account_binding_patch.start()
        self.addCleanup(self.account_binding_patch.stop)

    @staticmethod
    def reminder(stable_id: str, list_title: str, *, due_date: str = "2026-07-12") -> dict[str, object]:
        return {
            "stable_id": stable_id,
            "title": f"Reminder {stable_id}",
            "notes": "",
            "list_title": list_title,
            "list_id": f"list-{list_title}",
            "account_id": "icloud",
            "due_date": due_date,
            "due_at": None,
            "all_day": bool(due_date),
            "modified_at": "2026-07-11T00:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }

    @staticmethod
    def loaded_config(base: Path, payload: dict[str, object], *extra_args: str) -> dict[str, object]:
        config_path = base / "config.json"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        args = sync.build_parser().parse_args(
            ["--config", str(config_path), "sync", "--dry-run", *extra_args]
        )
        return sync.load_config(args)

    def test_list_policy_inherits_globals_and_overrides_one_list(self) -> None:
        config = sync.default_config()
        config.update(
            {
                "bidirectional": True,
                "delete_stale": True,
                "tasks_sync_undated": False,
                "conflict_policy": "newer_wins",
                "list_policies": sync.normalize_list_policies(
                    {
                        "Work": {
                            "direction": "google_to_apple",
                            "delete_propagation": False,
                            "sync_undated": True,
                            "conflict_policy": "skip",
                        }
                    }
                ),
            }
        )

        self.assertEqual(
            sync.list_sync_policy(config, "Personal"),
            {
                "direction": "bidirectional",
                "delete_propagation": True,
                "sync_undated": False,
                "conflict_policy": "newer_wins",
            },
        )
        self.assertEqual(
            sync.list_sync_policy(config, "Work"),
            {
                "direction": "google_to_apple",
                "delete_propagation": False,
                "sync_undated": True,
                "conflict_policy": "skip",
            },
        )

    def test_invalid_list_policy_config_fails_closed(self) -> None:
        invalid_policies = [
            ["not-an-object"],
            {"Work": {"direction": "sideways"}},
            {"Work": {"delete_propagation": "yes"}},
            {"Work": {"sync_undated": 1}},
            {"Work": {"conflict_policy": "merge"}},
            {"Work": {"typo_direction": "bidirectional"}},
        ]
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            for index, policies in enumerate(invalid_policies):
                with self.subTest(policies=policies):
                    config_path = base / f"config-{index}.json"
                    config_path.write_text(json.dumps({"list_policies": policies}), encoding="utf-8")
                    args = sync.build_parser().parse_args(
                        ["--config", str(config_path), "sync", "--dry-run"]
                    )
                    with self.assertRaises(SystemExit):
                        sync.load_config(args)

    def test_no_delete_stale_cli_overrides_per_list_delete_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = self.loaded_config(
                Path(tmp_name),
                {
                    "delete_stale": False,
                    "list_policies": {"Work": {"delete_propagation": True}},
                },
                "--no-delete-stale",
            )

        self.assertFalse(sync.list_allows_delete_propagation(config, "Work"))

    def test_undated_policy_filters_items_without_losing_empty_list_mirroring(self) -> None:
        config = sync.default_config()
        config["tasks_sync_undated"] = False
        config["list_policies"] = sync.normalize_list_policies(
            {
                "Work": {"sync_undated": True},
                "Personal": {"sync_undated": False},
            }
        )
        work = self.reminder("work-undated", "Work", due_date=None)
        personal = self.reminder("personal-undated", "Personal", due_date=None)

        with mock.patch.object(sync, "run_reminders_export", return_value=[work, personal]), mock.patch.object(
            sync,
            "run_reminders_lists_export",
            return_value=[{"title": "Work"}, {"title": "Personal"}, {"title": "Empty"}],
        ):
            reminders, desired, skipped = sync.build_desired_tasks(config)

        self.assertTrue(sync.reminders_export_needs_undated(config))
        self.assertEqual(reminders, [work])
        self.assertEqual(set(desired), {"Work", "Personal", "Empty"})
        self.assertEqual(len(desired["Work"]), 1)
        self.assertEqual(desired["Personal"], {})
        self.assertEqual(desired["Empty"], {})
        self.assertEqual(skipped, 0)

    def test_inbound_only_missing_list_is_not_created_but_existing_mirror_is_resolved(self) -> None:
        config = sync.default_config()
        config["list_policies"] = sync.normalize_list_policies(
            {
                "Existing inbound": {"direction": "google_to_apple"},
                "Missing inbound": {"direction": "google_to_apple"},
                "Missing outbound": {"direction": "apple_to_google"},
            }
        )

        class FakeClient:
            @staticmethod
            def list_tasklists() -> list[dict[str, str]]:
                return [{"id": "existing-list", "title": "Existing inbound"}]

        tasklists, missing = sync.inspect_tasklists_for_desired(
            FakeClient(),  # type: ignore[arg-type]
            {
                "Existing inbound": {},
                "Missing inbound": {},
                "Missing outbound": {},
            },
            config,
        )

        self.assertEqual(tasklists["Existing inbound"], "existing-list")
        self.assertNotIn("Missing inbound", tasklists)
        self.assertTrue(tasklists["Missing outbound"].startswith("planned:"))
        self.assertEqual(missing, ["Missing outbound"])

    def test_direction_filters_inbound_and_outbound_mutation_plans(self) -> None:
        config = sync.default_config()
        config["list_policies"] = sync.normalize_list_policies(
            {
                "Outbound": {"direction": "apple_to_google"},
                "Inbound": {"direction": "google_to_apple"},
            }
        )
        outbound_reminder = self.reminder("outbound", "Outbound")
        inbound_reminder = self.reminder("inbound", "Inbound")
        outbound_uid, outbound_body, outbound_digest = sync.build_task(outbound_reminder, config)
        inbound_uid, inbound_body, inbound_digest = sync.build_task(inbound_reminder, config)
        inbound_task = {**inbound_body, "id": "inbound-task", "title": "Changed in Google"}
        desired = {
            "Outbound": {outbound_uid: (outbound_body, outbound_digest, outbound_reminder)},
            "Inbound": {inbound_uid: (inbound_body, inbound_digest, inbound_reminder)},
        }
        tasklists = {"Outbound": "outbound-list", "Inbound": "inbound-list"}

        inbound_plan = sync.plan_google_task_changes_to_reminders(
            config,
            desired,
            tasklists,
            {"Outbound": {}, "Inbound": {inbound_uid: inbound_task}},
            {"Outbound": [], "Inbound": [inbound_task]},
            {"Outbound": [], "Inbound": []},
            {"version": 1, "events": {}, "tasks": {}},
            allow_deletes=True,
        )
        outbound_plan = sync.plan_google_task_outbound_mutations(
            config,
            desired,
            tasklists,
            {"Outbound": {}, "Inbound": {inbound_uid: inbound_task}},
            {"Outbound": {}, "Inbound": {}},
            {"Outbound": {}, "Inbound": {}},
            {"Outbound": {}, "Inbound": {"inbound-task": inbound_task}},
            {"Outbound": [], "Inbound": []},
            {"version": 1, "events": {}, "tasks": {}},
            set(inbound_plan["source_controlled"]),
            {},
            allow_deletes=True,
        )

        self.assertEqual([action["operation"] for action in inbound_plan["actions"]], ["update"])
        self.assertIn(("Inbound", inbound_uid), inbound_plan["source_controlled"])
        self.assertEqual([action["operation"] for action in outbound_plan], ["create"])

    def test_google_to_apple_direction_remains_authoritative_when_apple_is_newer(self) -> None:
        config = sync.default_config()
        config["list_policies"] = sync.normalize_list_policies(
            {"Inbox": {"direction": "google_to_apple", "conflict_policy": "newer_wins"}}
        )
        reminder = self.reminder("inbox", "Inbox")
        reminder["modified_at"] = "2026-07-11T02:00:00Z"
        uid, body, digest = sync.build_task(reminder, config)
        google_task = {
            **body,
            "id": "google-inbox",
            "title": "Google remains source",
            "updated": "2026-07-11T01:00:00Z",
        }
        tasklist_id = "inbox-list"
        state = {
            "version": 1,
            "events": {},
            "tasks": {
                sync.target_state_key(tasklist_id, uid): {
                    "tasklist_id": tasklist_id,
                    "tasklist_title": "Inbox",
                    "task_id": google_task["id"],
                    "digest": "older-apple-digest",
                    "google_digest": "older-google-digest",
                    "source_stable_id": reminder["stable_id"],
                }
            },
        }

        plan = sync.plan_google_task_changes_to_reminders(
            config,
            {"Inbox": {uid: (body, digest, reminder)}},
            {"Inbox": tasklist_id},
            {"Inbox": {uid: google_task}},
            {"Inbox": [google_task]},
            {"Inbox": []},
            state,
            allow_deletes=True,
        )

        self.assertEqual(plan["conflicts"], [])
        self.assertEqual([action["operation"] for action in plan["actions"]], ["update"])

    def test_newer_google_title_preserves_concurrent_apple_due_date(self) -> None:
        config = sync.default_config()
        config.update({"bidirectional": True, "conflict_policy": "newer_wins"})
        reminder = self.reminder("dated-concurrently", "Personal")
        reminder["modified_at"] = "2026-07-17T04:50:00Z"
        uid, body, digest = sync.build_task(reminder, config)
        google_task = {
            **body,
            "id": "google-newer-title",
            "title": "Changed in Google",
            "updated": "2026-07-17T04:54:18Z",
        }
        google_task.pop("due")
        tasklist_id = "personal-list"
        state = {
            "version": 1,
            "events": {},
            "tasks": {
                sync.target_state_key(tasklist_id, uid): {
                    "tasklist_id": tasklist_id,
                    "tasklist_title": "Personal",
                    "task_id": google_task["id"],
                    "digest": "older-apple-digest",
                    "google_digest": "older-google-digest",
                    "source_stable_id": reminder["stable_id"],
                }
            },
        }

        inbound = sync.plan_google_task_changes_to_reminders(
            config,
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": tasklist_id},
            {"Personal": {uid: google_task}},
            {"Personal": [google_task]},
            {"Personal": []},
            state,
            allow_deletes=True,
        )
        outbound = sync.plan_google_task_outbound_mutations(
            config,
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": tasklist_id},
            {"Personal": {uid: google_task}},
            {"Personal": {}},
            {"Personal": {}},
            {"Personal": {google_task["id"]: google_task}},
            {"Personal": []},
            state,
            set(inbound["source_controlled"]),
            {},
            allow_deletes=True,
        )

        self.assertEqual([operation["title"] for operation in inbound["operations"]], ["Changed in Google"])
        self.assertNotIn("clear_due", inbound["operations"][0])
        self.assertNotIn(("Personal", uid), inbound["source_controlled"])
        self.assertEqual([action["operation"] for action in outbound], ["update"])

    def test_missing_google_due_with_stale_source_digest_repairs_google_not_apple(self) -> None:
        config = sync.default_config()
        config.update({"bidirectional": True, "conflict_policy": "newer_wins"})
        reminder = self.reminder("dated-with-stale-digest", "Personal")
        reminder["modified_at"] = "2026-07-17T04:50:00Z"
        uid, body, digest = sync.build_task(reminder, config)
        google_task = {
            **body,
            "id": "google-missing-due",
            "notes": str(body["notes"]).replace(digest, "older-source-digest"),
            "updated": "2026-07-17T04:54:18Z",
        }
        google_task.pop("due")
        tasklist_id = "personal-list"
        state = {
            "version": 1,
            "events": {},
            "tasks": {
                sync.target_state_key(tasklist_id, uid): {
                    "tasklist_id": tasklist_id,
                    "tasklist_title": "Personal",
                    "task_id": google_task["id"],
                    "digest": "older-apple-digest",
                    "google_digest": "older-google-digest",
                    "source_stable_id": reminder["stable_id"],
                }
            },
        }

        inbound = sync.plan_google_task_changes_to_reminders(
            config,
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": tasklist_id},
            {"Personal": {uid: google_task}},
            {"Personal": [google_task]},
            {"Personal": []},
            state,
            allow_deletes=True,
        )
        outbound = sync.plan_google_task_outbound_mutations(
            config,
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": tasklist_id},
            {"Personal": {uid: google_task}},
            {"Personal": {}},
            {"Personal": {}},
            {"Personal": {google_task["id"]: google_task}},
            {"Personal": []},
            state,
            set(inbound["source_controlled"]),
            {},
            allow_deletes=True,
        )

        self.assertEqual(inbound["operations"], [])
        self.assertEqual(inbound["source_controlled"], set())
        self.assertEqual([action["operation"] for action in outbound], ["update"])

    def test_reopened_apple_reminder_reopens_older_completed_google_task_without_state(self) -> None:
        config = sync.default_config()
        config.update({"bidirectional": True, "conflict_policy": "newer_wins"})
        reminder = self.reminder("reopened", "Personal")
        reminder["modified_at"] = "2026-07-17T04:54:18Z"
        uid, body, digest = sync.build_task(reminder, config)
        completed_task = {
            **body,
            "id": "completed-task",
            "status": "completed",
            "updated": "2026-07-17T04:50:00Z",
        }
        desired = {"Personal": {uid: (body, digest, reminder)}}
        tasklists = {"Personal": "personal-list"}
        existing = {"Personal": {uid: completed_task}}
        state = {"version": 1, "events": {}, "tasks": {}}

        inbound = sync.plan_google_task_changes_to_reminders(
            config,
            desired,
            tasklists,
            existing,
            {"Personal": [completed_task]},
            {"Personal": []},
            state,
            allow_deletes=True,
        )
        outbound = sync.plan_google_task_outbound_mutations(
            config,
            desired,
            tasklists,
            existing,
            {"Personal": {}},
            {"Personal": {}},
            {"Personal": {"completed-task": completed_task}},
            {"Personal": []},
            state,
            set(inbound["source_controlled"]),
            {},
            allow_deletes=True,
        )

        self.assertEqual(inbound["operations"], [])
        self.assertEqual(inbound["conflicts"], [])
        self.assertEqual(inbound["blocked"], set())
        self.assertEqual([action["operation"] for action in outbound], ["update"])
        self.assertEqual(sync.task_body_for_patch(body, completed_task)["status"], "needsAction")

    def test_newer_completed_google_task_still_completes_active_apple_reminder_without_state(self) -> None:
        config = sync.default_config()
        config.update({"bidirectional": True, "conflict_policy": "newer_wins"})
        reminder = self.reminder("google-newer", "Personal")
        reminder["modified_at"] = "2026-07-17T04:50:00Z"
        uid, body, digest = sync.build_task(reminder, config)
        completed_task = {
            **body,
            "id": "completed-task",
            "status": "completed",
            "updated": "2026-07-17T04:54:18Z",
        }

        plan = sync.plan_google_task_changes_to_reminders(
            config,
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": "personal-list"},
            {"Personal": {uid: completed_task}},
            {"Personal": [completed_task]},
            {"Personal": []},
            {"version": 1, "events": {}, "tasks": {}},
            allow_deletes=True,
        )

        self.assertEqual(plan["conflicts"], [])
        self.assertEqual([operation.get("complete") for operation in plan["operations"]], [True])
        self.assertEqual([action["operation"] for action in plan["actions"]], ["complete"])

    def test_delete_propagation_is_scoped_per_list_in_both_directions(self) -> None:
        config = sync.default_config()
        config["bidirectional"] = True
        config["list_policies"] = sync.normalize_list_policies(
            {
                "Protected": {"delete_propagation": False},
                "Disposable": {"delete_propagation": True},
            }
        )
        state = {"version": 1, "events": {}, "tasks": {}}
        deleted_by_list: dict[str, list[dict[str, object]]] = {}
        for list_title, tasklist_id in (("Protected", "protected-list"), ("Disposable", "disposable-list")):
            uid = f"uid-{list_title}"
            task_id = f"task-{list_title}"
            state["tasks"][sync.target_state_key(tasklist_id, uid)] = {
                "tasklist_id": tasklist_id,
                "tasklist_title": list_title,
                "task_id": task_id,
                "source_stable_id": f"reminder-{list_title}",
            }
            deleted_by_list[list_title] = [
                {
                    "id": task_id,
                    "title": list_title,
                    "deleted": True,
                    "notes": (
                        "Synced from Apple Reminders.\n"
                        f"Source UID: {uid}\n"
                        "Source Digest: old"
                    ),
                }
            ]

        tasklists = {"Protected": "protected-list", "Disposable": "disposable-list"}
        inbound_plan = sync.plan_google_task_changes_to_reminders(
            config,
            {"Protected": {}, "Disposable": {}},
            tasklists,
            {"Protected": {}, "Disposable": {}},
            {"Protected": [], "Disposable": []},
            deleted_by_list,
            state,
            allow_deletes=True,
        )
        outbound_plan = sync.plan_google_task_outbound_mutations(
            config,
            {"Protected": {}, "Disposable": {}},
            tasklists,
            {"Protected": {}, "Disposable": {}},
            {"Protected": {}, "Disposable": {}},
            {"Protected": {}, "Disposable": {}},
            {"Protected": {}, "Disposable": {}},
            {"Protected": [], "Disposable": []},
            state,
            set(),
            {},
            allow_deletes=True,
        )

        self.assertEqual([action["operation"] for action in inbound_plan["actions"]], ["delete"])
        self.assertEqual([action["operation"] for action in outbound_plan], ["delete"])

    def test_deleted_tombstone_is_ignored_when_same_uid_is_active(self) -> None:
        config = sync.default_config()
        config["bidirectional"] = True
        reminder = self.reminder("recreated", "Personal")
        uid, body, digest = sync.build_task(reminder, config)
        tasklist_id = "personal-list"
        active_task = {**body, "id": "active-recreated-task"}
        deleted_task = {
            "id": "old-deleted-task",
            "title": body["title"],
            "deleted": True,
            "notes": (
                "Synced from Apple Reminders.\n"
                f"Source UID: {uid}\n"
                "Source Digest: old"
            ),
        }
        state = {"version": 1, "events": {}, "tasks": {}}
        sync.save_task_state(
            state,
            tasklist_id,
            uid,
            active_task,
            digest,
            str(body["title"]),
            reminder,
            config,
            "Personal",
        )

        plan = sync.plan_google_task_changes_to_reminders(
            config,
            {"Personal": {uid: (body, digest, reminder)}},
            {"Personal": tasklist_id},
            {"Personal": {uid: active_task}},
            {"Personal": [active_task]},
            {"Personal": [deleted_task]},
            state,
            allow_deletes=True,
        )

        self.assertEqual(plan["operations"], [])
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["blocked"], set())

    def test_conflict_policy_is_resolved_per_list(self) -> None:
        config = sync.default_config()
        config["list_policies"] = sync.normalize_list_policies(
            {
                "Manual": {"direction": "bidirectional", "conflict_policy": "skip"},
                "Automatic": {"direction": "bidirectional", "conflict_policy": "newer_wins"},
            }
        )
        desired: dict[str, dict[str, tuple[dict[str, object], str, dict[str, object]]]] = {}
        existing: dict[str, dict[str, dict[str, object]]] = {}
        state: dict[str, object] = {"version": 1, "events": {}, "tasks": {}}
        tasklists = {"Manual": "manual-list", "Automatic": "automatic-list"}
        for list_title, tasklist_id in tasklists.items():
            reminder = self.reminder(list_title.lower(), list_title)
            uid, body, digest = sync.build_task(reminder, config)
            task = {
                **body,
                "id": f"task-{list_title}",
                "title": f"Google {list_title}",
                "updated": "2026-07-11T01:00:00Z",
            }
            desired[list_title] = {uid: (body, digest, reminder)}
            existing[list_title] = {uid: task}
            state["tasks"][sync.target_state_key(tasklist_id, uid)] = {
                "tasklist_id": tasklist_id,
                "tasklist_title": list_title,
                "task_id": task["id"],
                "digest": "previous-apple-digest",
                "google_digest": "previous-google-digest",
                "source_stable_id": reminder["stable_id"],
            }

        plan = sync.plan_google_task_changes_to_reminders(
            config,
            desired,
            tasklists,
            existing,
            {list_title: list(tasks.values()) for list_title, tasks in existing.items()},
            {"Manual": [], "Automatic": []},
            state,
            allow_deletes=True,
        )

        self.assertEqual(plan["conflicts"], ["Google Manual"])
        self.assertEqual([action["operation"] for action in plan["actions"]], ["update"])

    def test_actual_sync_writes_only_apple_to_google_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = sync.default_config()
            config.update(
                {
                    "state_path": str(Path(tmp_name) / "state.json"),
                    "status_path": str(Path(tmp_name) / "status.json"),
                    "delete_stale": False,
                    "tasks_complete_stale": False,
                    "verify_title_due_after_sync": False,
                    "list_policies": sync.normalize_list_policies(
                        {
                            "Outbound": {"direction": "apple_to_google"},
                            "Inbound": {"direction": "google_to_apple"},
                        }
                    ),
                }
            )
            outbound_reminder = self.reminder("outbound-live", "Outbound")
            inbound_reminder = self.reminder("inbound-live", "Inbound")
            outbound_uid, outbound_body, outbound_digest = sync.build_task(outbound_reminder, config)
            inbound_uid, inbound_body, inbound_digest = sync.build_task(inbound_reminder, config)

            class FakeClient:
                inserted_lists: list[str] = []

                def __init__(self, _config: dict[str, object]) -> None:
                    pass

                @staticmethod
                def list_tasklists() -> list[dict[str, str]]:
                    return [
                        {"id": "outbound-list", "title": "Outbound"},
                        {"id": "inbound-list", "title": "Inbound"},
                    ]

                @staticmethod
                def list_tasks(_tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                    if not show_deleted:
                        raise AssertionError
                    return []

                def insert_task(self, tasklist_id: str, payload: dict[str, object]) -> dict[str, object]:
                    type(self).inserted_lists.append(tasklist_id)
                    return {**payload, "id": f"created-{tasklist_id}"}

            with mock.patch.object(sync, "GoogleTasksClient", FakeClient), mock.patch.object(
                sync,
                "build_desired_tasks",
                return_value=(
                    [outbound_reminder, inbound_reminder],
                    {
                        "Outbound": {outbound_uid: (outbound_body, outbound_digest, outbound_reminder)},
                        "Inbound": {inbound_uid: (inbound_body, inbound_digest, inbound_reminder)},
                    },
                    0,
                ),
            ):
                sync.run_tasks_sync(config)

        self.assertEqual(FakeClient.inserted_lists, ["outbound-list"])

    def test_actual_sync_deletes_only_lists_that_allow_propagation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = sync.default_config()
            config.update(
                {
                    "state_path": str(Path(tmp_name) / "state.json"),
                    "status_path": str(Path(tmp_name) / "status.json"),
                    "allow_empty_source_delete": True,
                    "tasks_complete_stale": False,
                    "verify_title_due_after_sync": False,
                    "max_destructive_changes": 10,
                    "max_destructive_ratio": 1.0,
                    "list_policies": sync.normalize_list_policies(
                        {
                            "Protected": {"delete_propagation": False},
                            "Disposable": {"delete_propagation": True},
                        }
                    ),
                }
            )

            def stale_task(list_title: str) -> dict[str, object]:
                return {
                    "id": f"task-{list_title}",
                    "title": list_title,
                    "status": "needsAction",
                    "notes": (
                        "Synced from Apple Reminders.\n"
                        f"Source UID: uid-{list_title}\n"
                        "Source Digest: old"
                    ),
                }

            class FakeClient:
                deleted_tasklists: list[str] = []

                def __init__(self, _config: dict[str, object]) -> None:
                    pass

                @staticmethod
                def list_tasklists() -> list[dict[str, str]]:
                    return [
                        {"id": "protected-list", "title": "Protected"},
                        {"id": "disposable-list", "title": "Disposable"},
                    ]

                @staticmethod
                def list_tasks(tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                    if not show_deleted:
                        raise AssertionError
                    title = "Protected" if tasklist_id == "protected-list" else "Disposable"
                    return [stale_task(title)]

                def delete_task(self, tasklist_id: str, _task_id: str) -> None:
                    type(self).deleted_tasklists.append(tasklist_id)

            with mock.patch.object(sync, "GoogleTasksClient", FakeClient), mock.patch.object(
                sync,
                "build_desired_tasks",
                return_value=([], {"Protected": {}, "Disposable": {}}, 0),
            ):
                sync.run_tasks_sync(config)

        self.assertEqual(FakeClient.deleted_tasklists, ["disposable-list"])


class BlockedPlanAutoApprovalTests(unittest.TestCase):
    def test_streak_counts_only_identical_consecutive_fingerprints(self) -> None:
        fp, streak = sync.next_blocked_plan_streak("", 0, "aaa")
        self.assertEqual((fp, streak), ("aaa", 1))
        fp, streak = sync.next_blocked_plan_streak(fp, streak, "aaa")
        self.assertEqual((fp, streak), ("aaa", 2))
        fp, streak = sync.next_blocked_plan_streak(fp, streak, "bbb")
        self.assertEqual((fp, streak), ("bbb", 1))
        fp, streak = sync.next_blocked_plan_streak(fp, streak, "")
        self.assertEqual((fp, streak), ("", 0))

    def test_auto_approval_requires_positive_config_and_streak(self) -> None:
        self.assertFalse(sync.should_auto_approve_blocked_plan({"auto_approve_destructive_loops": 0}, 99))
        self.assertFalse(sync.should_auto_approve_blocked_plan({"auto_approve_destructive_loops": 3}, 2))
        self.assertTrue(sync.should_auto_approve_blocked_plan({"auto_approve_destructive_loops": 3}, 3))
        self.assertTrue(sync.should_auto_approve_blocked_plan({}, 1) is False)

    def test_generated_token_for_stable_fingerprint_is_accepted(self) -> None:
        config = {"destructive_approval_ttl_seconds": 600}
        plan = {"fingerprint": "f" * 64}
        token = sync.mutation_plan_approval_token(plan)
        accepted, reason = sync.validate_mutation_plan_approval(config, plan, token)
        self.assertTrue(accepted)
        self.assertEqual(reason, "approved")


class MutationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account_binding_patch = mock.patch.object(
            sync,
            "resolve_sync_account_binding",
            return_value=AccountBindingIsolationTests.binding(
                "TENANT_A_ONLY_APPLE_TEST_ACCOUNT",
                "TENANT_A_ONLY_GOOGLE_TEST_ACCOUNT",
            ),
        )
        self.account_binding_patch.start()
        self.addCleanup(self.account_binding_patch.stop)

    def config(self, base: Path) -> dict[str, object]:
        config = sync.default_config()
        config.update(
            {
                "state_path": str(base / "state.json"),
                "status_path": str(base / "status.json"),
                "bidirectional": False,
                "tasks_complete_stale": False,
                "verify_title_due_after_sync": False,
                "max_destructive_changes": 2,
                "max_destructive_ratio": 0.5,
                "destructive_approval_ttl_seconds": 600,
                "_mutation_plan_approval": "",
            }
        )
        return config

    def test_fingerprint_is_deterministic_and_counts_two_way_destructive_actions(self) -> None:
        actions = [
            sync.planned_mutation("google_tasks", "dedupe_delete", ["list", "task-3"], destructive=True),
            sync.planned_mutation("apple_reminders", "complete", ["list", "reminder-1"], destructive=True),
            sync.planned_mutation("google_tasks", "update", ["list", "task-4"]),
            sync.planned_mutation("apple_reminders", "delete", ["list", "reminder-2"], destructive=True),
        ]

        first = sync.build_mutation_plan(actions, population=8)
        second = sync.build_mutation_plan(list(reversed(actions)), population=8)

        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["destructive_count"], 3)
        self.assertEqual(first["destructive_counts"]["apple_reminders.complete"], 1)
        self.assertEqual(first["destructive_counts"]["apple_reminders.delete"], 1)
        self.assertEqual(first["destructive_counts"]["google_tasks.dedupe_delete"], 1)

    def test_count_and_ratio_boundaries_block_only_when_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = self.config(Path(tmp_name))
            actions = [
                sync.planned_mutation("google_tasks", "delete", index, destructive=True)
                for index in range(2)
            ]
            boundary = sync.build_mutation_plan(actions, population=4)
            self.assertEqual(sync.mutation_plan_limit_reasons(config, boundary), [])

            count_config = dict(config)
            count_config["max_destructive_changes"] = 1
            self.assertIn("destructive_count_limit", sync.mutation_plan_limit_reasons(count_config, boundary))

            ratio_config = dict(config)
            ratio_config["max_destructive_ratio"] = 0.49
            self.assertIn("destructive_ratio_limit", sync.mutation_plan_limit_reasons(ratio_config, boundary))

    def test_approval_requires_matching_unexpired_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = self.config(Path(tmp_name))
            plan = sync.build_mutation_plan(
                [sync.planned_mutation("google_tasks", "delete", "task", destructive=True)],
                population=1,
            )
            issued_at = dt.datetime(2026, 7, 11, 0, 0, tzinfo=dt.timezone.utc)
            token = sync.mutation_plan_approval_token(plan, issued_at=issued_at)

            accepted, reason = sync.validate_mutation_plan_approval(
                config,
                plan,
                token,
                now=issued_at + dt.timedelta(seconds=599),
            )
            self.assertTrue(accepted)
            self.assertEqual(reason, "approved")

            other = sync.build_mutation_plan(
                [sync.planned_mutation("google_tasks", "delete", "other", destructive=True)],
                population=1,
            )
            self.assertEqual(
                sync.validate_mutation_plan_approval(config, other, token, now=issued_at)[1],
                "approval_fingerprint_mismatch",
            )
            self.assertEqual(
                sync.validate_mutation_plan_approval(
                    config,
                    plan,
                    token,
                    now=issued_at + dt.timedelta(seconds=601),
                )[1],
                "approval_expired",
            )

    def test_matching_fresh_approval_allows_destructive_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = self.config(Path(tmp_name))
            config["max_destructive_changes"] = 0
            config["max_destructive_ratio"] = 0.0
            plan = sync.build_mutation_plan(
                [sync.planned_mutation("google_tasks", "delete", "task", destructive=True)],
                population=1,
            )
            now = dt.datetime(2026, 7, 11, 0, 0, tzinfo=dt.timezone.utc)
            config["_mutation_plan_approval"] = sync.mutation_plan_approval_token(plan, issued_at=now)

            sync.enforce_mutation_plan(config, plan, dry_run=False, now=now + dt.timedelta(seconds=30))

            self.assertFalse((Path(tmp_name) / "status.json").exists())

    def test_mass_loss_blocks_before_any_google_or_apple_mutator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            config = self.config(base)
            config["max_destructive_changes"] = 2
            config["max_destructive_ratio"] = 0.2
            reminder = {
                "stable_id": "private-reminder-0",
                "title": "Private reminder title",
                "notes": "private notes",
                "list_title": "Personal",
                "list_id": "list-local",
                "account_id": "icloud",
                "due_date": "2026-07-12",
                "due_at": None,
                "all_day": True,
                "modified_at": "2026-07-11T00:00:00Z",
                "completed_at": None,
                "is_completed": False,
            }
            uid, body, digest = sync.build_task(reminder, config)
            tasks = [{**body, "id": "task-0"}]
            for index in range(1, 10):
                tasks.append(
                    {
                        "id": f"task-{index}",
                        "title": f"Sensitive stale title {index}",
                        "notes": (
                            "Synced from Apple Reminders.\n"
                            f"Source UID: stale-uid-{index}\n"
                            f"Source Digest: stale-digest-{index}"
                        ),
                        "status": "needsAction",
                    }
                )

            class FakeClient:
                mutations: list[str] = []

                def __init__(self, _config: dict[str, object]) -> None:
                    pass

                def list_tasklists(self) -> list[dict[str, str]]:
                    return [{"id": "tasklist-1", "title": "Personal"}]

                def list_tasks(self, _tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                    self.assert_read_only(show_deleted)
                    return tasks

                @staticmethod
                def assert_read_only(show_deleted: bool) -> None:
                    if not show_deleted:
                        raise AssertionError("preflight must read the complete task snapshot")

                def insert_tasklist(self, *_args: object) -> dict[str, str]:
                    self.mutations.append("insert_tasklist")
                    return {"id": "unexpected"}

                def insert_task(self, *_args: object) -> dict[str, str]:
                    self.mutations.append("insert_task")
                    return {"id": "unexpected"}

                def patch_task(self, *_args: object) -> dict[str, str]:
                    self.mutations.append("patch_task")
                    return {"id": "unexpected"}

                def delete_task(self, *_args: object) -> None:
                    self.mutations.append("delete_task")

            with (
                mock.patch.object(sync, "GoogleTasksClient", FakeClient),
                mock.patch.object(
                    sync,
                    "build_desired_tasks",
                    return_value=([reminder], {"Personal": {uid: (body, digest, reminder)}}, 0),
                ),
                mock.patch.object(sync, "run_reminders_apply") as apple_mutator,
            ):
                with self.assertRaises(sync.MutationPlanApprovalRequired):
                    sync.run_tasks_sync(config)

            self.assertEqual(FakeClient.mutations, [])
            apple_mutator.assert_not_called()
            status = json.loads((base / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "blocked_mutation_plan")
            visible = json.dumps(status, ensure_ascii=False)
            self.assertNotIn("Private reminder title", visible)
            self.assertNotIn("Sensitive stale title", visible)
            self.assertEqual(status["mutation_plan"]["destructive_count"], 9)

    def test_normal_small_change_keeps_existing_insert_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            config = self.config(base)
            reminder = {
                "stable_id": "reminder-new",
                "title": "Add one task",
                "notes": "",
                "list_title": "Personal",
                "list_id": "list-local",
                "account_id": "icloud",
                "due_date": "2026-07-12",
                "due_at": None,
                "modified_at": "2026-07-11T00:00:00Z",
                "completed_at": None,
                "is_completed": False,
            }
            uid, body, digest = sync.build_task(reminder, config)

            class FakeClient:
                inserts = 0

                def __init__(self, _config: dict[str, object]) -> None:
                    pass

                def list_tasklists(self) -> list[dict[str, str]]:
                    return [{"id": "tasklist-1", "title": "Personal"}]

                def list_tasks(self, _tasklist_id: str, show_deleted: bool = False) -> list[dict[str, object]]:
                    if not show_deleted:
                        raise AssertionError
                    return []

                def insert_task(self, _tasklist_id: str, payload: dict[str, object]) -> dict[str, object]:
                    type(self).inserts += 1
                    return {**payload, "id": "created-task"}

            with (
                mock.patch.object(sync, "GoogleTasksClient", FakeClient),
                mock.patch.object(
                    sync,
                    "build_desired_tasks",
                    return_value=([reminder], {"Personal": {uid: (body, digest, reminder)}}, 0),
                ),
            ):
                sync.run_tasks_sync(config)

            self.assertEqual(FakeClient.inserts, 1)
            state = json.loads((base / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["tasks"]), 1)

    def test_calendar_mass_loss_blocks_before_google_mutator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            config = self.config(base)
            config["max_destructive_changes"] = 2
            config["max_destructive_ratio"] = 0.2
            reminder = {
                "stable_id": "calendar-reminder-0",
                "title": "Private calendar reminder",
                "notes": "private notes",
                "list_title": "Personal",
                "list_id": "list-local",
                "account_id": "icloud",
                "due_date": "2026-07-12",
                "due_at": None,
                "all_day": True,
                "modified_at": "2026-07-11T00:00:00Z",
                "completed_at": None,
                "is_completed": False,
            }
            uid, body, digest = sync.build_event(reminder, config)
            events = [{**body, "id": "event-0"}]
            for index in range(1, 10):
                events.append(
                    {
                        "id": f"event-{index}",
                        "summary": f"Sensitive calendar title {index}",
                        "extendedProperties": {
                            "private": {
                                sync.UID_KEY: f"stale-calendar-uid-{index}",
                                sync.DIGEST_KEY: f"digest-{index}",
                            }
                        },
                    }
                )

            class FakeCalendarClient:
                mutations: list[str] = []

                def __init__(self, _config: dict[str, object]) -> None:
                    pass

                def list_synced_events(self, _calendar_id: str) -> list[dict[str, object]]:
                    return events

                def insert_event(self, *_args: object) -> dict[str, str]:
                    self.mutations.append("insert_event")
                    return {"id": "unexpected"}

                def update_event(self, *_args: object) -> dict[str, str]:
                    self.mutations.append("update_event")
                    return {"id": "unexpected"}

                def delete_event(self, *_args: object) -> None:
                    self.mutations.append("delete_event")

            with (
                mock.patch.object(sync, "GoogleCalendarClient", FakeCalendarClient),
                mock.patch.object(
                    sync,
                    "build_desired_events",
                    return_value=([reminder], {uid: (body, digest, reminder)}, 0),
                ),
                mock.patch.object(sync, "run_reminders_apply") as apple_mutator,
            ):
                with self.assertRaises(sync.MutationPlanApprovalRequired):
                    sync.run_calendar_sync(config)

            self.assertEqual(FakeCalendarClient.mutations, [])
            apple_mutator.assert_not_called()
            status = json.loads((base / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["mutation_plan"]["destructive_count"], 9)
            visible = json.dumps(status, ensure_ascii=False)
            self.assertNotIn("Private calendar reminder", visible)
            self.assertNotIn("Sensitive calendar title", visible)

    def test_no_delete_stale_excludes_two_way_deletes_and_dedupe(self) -> None:
        config = sync.default_config()
        config["bidirectional"] = True
        reminder = {
            "stable_id": "active-reminder",
            "title": "Still active",
            "notes": "",
            "list_title": "Personal",
            "list_id": "list-local",
            "account_id": "icloud",
            "due_date": "2026-07-12",
            "due_at": None,
            "modified_at": "2026-07-11T00:00:00Z",
            "completed_at": None,
            "is_completed": False,
        }
        active_uid, active_body, active_digest = sync.build_task(reminder, config)
        completed_google_task = {
            **active_body,
            "id": "completed-task",
            "status": "completed",
            "updated": "2026-07-13T00:00:00Z",
        }
        deleted = {
            "id": "deleted-task",
            "title": "Deleted",
            "deleted": True,
            "notes": "Synced from Apple Reminders.\nSource UID: uid-1\nSource Digest: digest-1",
        }
        state = {
            "version": 1,
            "events": {},
            "tasks": {
                sync.target_state_key("tasklist-1", "uid-1"): {
                    "tasklist_id": "tasklist-1",
                    "tasklist_title": "Personal",
                    "task_id": "deleted-task",
                    "source_stable_id": "reminder-1",
                }
            },
        }
        bidirectional = sync.plan_google_task_changes_to_reminders(
            config,
            {"Personal": {active_uid: (active_body, active_digest, reminder)}},
            {"Personal": "tasklist-1"},
            {"Personal": {active_uid: completed_google_task}},
            {"Personal": [completed_google_task]},
            {"Personal": [deleted]},
            state,
            allow_deletes=False,
        )
        outbound = sync.plan_google_task_outbound_mutations(
            config,
            {"Personal": {}},
            {"Personal": "tasklist-1"},
            {"Personal": {}},
            {"Personal": {}},
            {"Personal": {}},
            {"Personal": {}},
            {"Personal": [{"id": "duplicate"}]},
            state,
            set(),
            {},
            allow_deletes=False,
        )

        self.assertEqual(bidirectional["actions"], [])
        self.assertIn(("Personal", active_uid), bidirectional["blocked"])
        self.assertEqual(outbound, [])


SETUP_PATH = Path(__file__).resolve().parent / "setup-new-mac.sh"


def setup_shell_function(name: str) -> str:
    """Return one function definition from setup-new-mac.sh.

    The installer runs its whole install flow at import time, so the tests
    execute the individual functions instead of sourcing the file.
    """

    source = SETUP_PATH.read_text(encoding="utf-8")
    start = source.index(f"\n{name}() {{\n") + 1
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def setup_shell_assignments(*names: str) -> str:
    source = SETUP_PATH.read_text(encoding="utf-8")
    wanted = [f"{name}=" for name in names]
    return "\n".join(
        line for line in source.splitlines() if any(line.startswith(prefix) for prefix in wanted)
    )


class LaunchAgentLogPrivacyTests(unittest.TestCase):
    """The sync logs hold Apple Reminders titles; keep them out of shared /tmp."""

    def run_shell(self, body: str, env: dict[str, str], cwd: str) -> subprocess.CompletedProcess[str]:
        script = Path(cwd) / "harness.sh"
        script.write_text(body, encoding="utf-8")
        full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        full_env.update(env)
        return subprocess.run(
            ["bash", str(script)],
            env=full_env,
            cwd=cwd,
            capture_output=True,
            text=True,
        )

    def prepare_logs_harness(self, legacy_out: Path, legacy_err: Path, extra: str = "") -> str:
        return "\n".join(
            [
                "set -euo pipefail",
                'info() { printf "INFO: %s\\n" "$*"; }',
                'warn() { printf "WARN: %s\\n" "$*" >&2; }',
                'die() { printf "ERROR: %s\\n" "$*" >&2; exit 3; }',
                setup_shell_assignments(
                    "LOG_DIR", "STDOUT_LOG", "STDERR_LOG", "LEGACY_STDOUT_LOG", "LEGACY_STDERR_LOG", "LOG_MAX_BYTES"
                ),
                # Never let the test delete the real user's /tmp history.
                f'LEGACY_STDOUT_LOG="{legacy_out}"',
                f'LEGACY_STDERR_LOG="{legacy_err}"',
                setup_shell_function("prepare_runtime_logs"),
                extra,
                "prepare_runtime_logs",
            ]
        )

    def test_prepare_runtime_logs_writes_only_into_a_private_user_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            home = base / "home"
            home.mkdir()
            legacy_out = base / "legacy.out.log"
            legacy_err = base / "legacy.err.log"
            legacy_out.write_text("CANARY_REMINDER_TITLE\n", encoding="utf-8")
            legacy_err.write_text("Sync loop error: CANARY_REMINDER_TITLE\n", encoding="utf-8")
            legacy_out.chmod(0o644)

            result = self.run_shell(
                self.prepare_logs_harness(legacy_out, legacy_err), {"HOME": str(home)}, tmp_name
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            log_dir = home / "Library" / "Logs" / "icloud-reminders-google-sync"
            self.assertTrue(log_dir.is_dir())
            self.assertEqual(stat.S_IMODE(log_dir.stat().st_mode), 0o700)
            for name in ("sync.out.log", "sync.err.log"):
                log_path = log_dir / name
                self.assertTrue(log_path.is_file(), name)
                self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600, name)
            # The world-readable history from the old /tmp layout is retired.
            self.assertFalse(legacy_out.exists())
            self.assertFalse(legacy_err.exists())

    def test_prepare_runtime_logs_refuses_a_planted_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            home = base / "home"
            log_dir = home / "Library" / "Logs" / "icloud-reminders-google-sync"
            log_dir.mkdir(parents=True)
            victim = base / "victim.txt"
            victim.write_text("owned by someone else\n", encoding="utf-8")
            victim.chmod(0o644)
            (log_dir / "sync.out.log").symlink_to(victim)

            result = self.run_shell(
                self.prepare_logs_harness(base / "legacy.out.log", base / "legacy.err.log"),
                {"HOME": str(home)},
                tmp_name,
            )

            self.assertEqual(result.returncode, 3, result.stdout)
            self.assertIn("symlink", result.stderr)
            # The chmod never followed the link.
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
            self.assertEqual(victim.read_text(encoding="utf-8"), "owned by someone else\n")

    def test_prepare_runtime_logs_still_rotates_oversized_logs_privately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            base = Path(tmp_name)
            home = base / "home"
            log_dir = home / "Library" / "Logs" / "icloud-reminders-google-sync"
            log_dir.mkdir(parents=True)
            current = log_dir / "sync.out.log"
            current.write_text("x" * 64, encoding="utf-8")
            current.chmod(0o644)

            result = self.run_shell(
                self.prepare_logs_harness(base / "legacy.out.log", base / "legacy.err.log"),
                {"HOME": str(home), "ICLOUD_SYNC_LOG_MAX_BYTES": "32"},
                tmp_name,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rotated = log_dir / "sync.out.log.1"
            self.assertTrue(rotated.is_file())
            self.assertEqual(stat.S_IMODE(rotated.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(current.stat().st_mode), 0o600)
            self.assertEqual(current.read_text(encoding="utf-8"), "")

    def reminders_access_harness(self, swift_status: int) -> str:
        return "\n".join(
            [
                "set -euo pipefail",
                'info() { printf "INFO: %s\\n" "$*"; }',
                'warn() { printf "WARN: %s\\n" "$*" >&2; }',
                'die() { printf "ERROR: %s\\n" "$*" >&2; exit 3; }',
                'EXPORTER="/nonexistent/RemindersExport.swift"',
                "swift() {",
                '  printf \'[{"title":"CANARY_LIST_TITLE"}]\\n\'',
                '  printf "exporter CANARY_ERROR\\n" >&2',
                f"  return {swift_status}",
                "}",
                setup_shell_function("check_reminders_access"),
                "check_reminders_access",
            ]
        )

    def test_reminders_list_export_never_lands_on_a_fixed_tmp_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            scratch = Path(tmp_name) / "tmpdir"
            scratch.mkdir()

            result = self.run_shell(
                self.reminders_access_harness(0), {"TMPDIR": str(scratch)}, tmp_name
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CANARY_LIST_TITLE", result.stdout)
            self.assertFalse(Path("/tmp/icloud-reminders-google-sync-lists.json").exists())
            self.assertFalse(Path("/tmp/icloud-reminders-google-sync-lists.err").exists())
            # Nothing survives the run, so nothing stays readable to other UIDs.
            self.assertEqual(sorted(p.name for p in scratch.iterdir()), [])

    def test_reminders_list_export_cleans_up_on_the_failure_path_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            scratch = Path(tmp_name) / "tmpdir"
            scratch.mkdir()

            result = self.run_shell(
                self.reminders_access_harness(1), {"TMPDIR": str(scratch)}, tmp_name
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("CANARY_ERROR", result.stderr)
            self.assertEqual(sorted(p.name for p in scratch.iterdir()), [])

    def test_run_loop_startup_reprivatises_a_relaxed_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            log_path = Path(tmp_name) / "sync.out.log"
            log_path.write_text("", encoding="utf-8")
            log_path.chmod(0o644)

            with log_path.open("a", encoding="utf-8") as handle:
                sync.harden_log_stream_mode(handle)

            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_log_hardening_leaves_non_files_alone(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(write_fd, "w") as handle:
                write_fd = -1
                sync.harden_log_stream_mode(handle)
        finally:
            os.close(read_fd)
            if write_fd != -1:
                os.close(write_fd)

        # A closed or already private stream must not raise either.
        sync.harden_log_stream_mode(io.StringIO())


if __name__ == "__main__":
    unittest.main()
