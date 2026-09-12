# Agent Instructions

`icloud-reminders-google-sync` is a Mac-local bridge between Apple Reminders and
Google Tasks. It is an independent source repository. EventKit access, Google
desktop credentials, LaunchAgent changes, and real synchronization stay on the
user's Mac and are never proved by source CI alone.

## Product Contract

- Apple Reminders and Google Tasks stay synchronized without deleting stale
  items accidentally.
- Fresh-machine setup uses `--no-delete-stale` until the local state map has
  been rebuilt safely.
- Only fix sync behavior backed by a reproducible case and a regression test.
- Google credentials, refresh tokens, sync state, logs, and exported task or
  reminder data stay outside the repository.

## Source And Runtime Ownership

- Source repository: the root of this Git checkout.
- Immutable runtime releases:
  `~/.local/share/icloud-reminders-google-sync/releases/<commit>/`.
- Stable runtime pointer: `~/.local/share/icloud-reminders-google-sync/current`.
- Config, credentials, state, and status:
  `~/.config/icloud-reminders-google-sync/`.
- LaunchAgent: `~/Library/LaunchAgents/com.icloud-reminders-google-sync.plist`.
- Logs: `~/Library/Logs/icloud-reminders-google-sync/sync.out.log` and
  `~/Library/Logs/icloud-reminders-google-sync/sync.err.log`, in a `0700`
  directory. Reminder and task titles appear in them, so they never go back to
  `/tmp`.

The LaunchAgent must execute the stable runtime pointer. It must not refer to a
task branch, Codex worktree, Downloads folder, migration extraction folder, or
other disposable checkout.

## Git Workflow

- Inspect status, branch, origin, and upstream before editing.
- Work on `codex/<task>`, preserve unrelated changes, and stage explicit paths.
- Keep `origin` equal to `syncweave-labs/reminders-task-bridge`.
- Push a reviewed CI-green branch, merge to `main`, and update the local main
  checkout before installing a runtime release.
- Never install or restart from an arbitrary branch, dirty worktree, or
  unpushed commit.

## Release And Installation

`setup-new-mac.sh` changes local config, runtime source, Google auth state, and
the user LaunchAgent. Before any of those mutations it runs:

```sh
DEPLOY_EXPECTED_COMMIT=<full-reviewed-main-sha> \
  bash scripts/check-release-source.sh
```

The gate requires a clean `main`, the expected GitHub origin, and equality
between `HEAD`, local `origin/main`, live origin main, and
`DEPLOY_EXPECTED_COMMIT`. Narrow emergency overrides require
`DEPLOY_GIT_OVERRIDE_REASON`; the expected origin is never overridable.

A first machine may use a migration bundle created from verified main. Bundle
bootstrap requires its generated manifest/checksums plus the explicit
`DEPLOY_GIT_ALLOW_BOOTSTRAP=1` audited override. This is the only supported
non-Git installation source.

Do not run setup, load/restart the LaunchAgent, authenticate Google, or execute
a live sync unless the user requested that live operation. Source-only work may
test that the entrypoint fails closed before mutation.

## Structure

- `icloud_reminders_google_sync.py`: sync engine and CLI.
- `RemindersExport.swift`: reads Apple Reminders.
- `RemindersApply.swift`: applies changes back to Apple Reminders.
- `google-tasks-manager.command`: Finder/Terminal entrypoint for diagnosis,
  online Google checks, confirmed reconnect, safe state rebuild, and LaunchAgent
  restart. Recovery must back up private files before mutation and must keep
  deletion propagation disabled while rebuilding state.
- `setup-new-mac.sh`: verified release installer and first-sync flow.
- `make-migration-bundle.sh`: verified first-machine bundle helper.
- `scripts/check-release-source.sh`: fail-closed release source gate.
- `scripts/test-release-source-gate.sh`: isolated gate self-test.
- `test_icloud_reminders_google_sync.py`: local regression tests.

## Required Source Checks

Run from this repository:

```sh
python3 -B -m unittest test_icloud_reminders_google_sync.py
python3 -B -m py_compile icloud_reminders_google_sync.py test_icloud_reminders_google_sync.py
bash -n setup-new-mac.sh make-migration-bundle.sh scripts/*.sh
bash scripts/test-release-source-gate.sh
```

Read-only runtime diagnosis is allowed when relevant:

```sh
/opt/homebrew/bin/python3 \
  ~/.local/share/icloud-reminders-google-sync/current/icloud_reminders_google_sync.py \
  doctor
```

Only run a live `sync` when the user explicitly asks to change Apple Reminders
or Google Tasks state.
