# Agent Instructions

`icloud-reminders-google-sync` is a Mac-local bridge between Apple Reminders and
Google Tasks. It is an independent source repository. EventKit access, Google
desktop credentials, LaunchAgent changes, and real synchronization stay on the
user's Mac and are never proved by source CI alone.

## Product Contract

- Apple Reminders and Google Tasks stay synchronized without deleting stale
  items accidentally.
- A plan over the destructive limits never stops the whole sync and is never
  written unasked: the scheduler holds only deletions and completions, asks the
  signed-in user once the plan has settled, and applies exactly the destructive
  set that was approved. Titles appear only in that dialog or the manager,
  never in `status.json`, argv, or a fingerprint.
- Fresh-machine setup uses `--no-delete-stale` until the local state map has
  been rebuilt safely.
- Only fix sync behavior backed by a reproducible case and a regression test.
- Google credentials, refresh tokens, sync state, logs, and exported task or
  reminder data stay outside the repository.
- `미리알림 날짜.app` changes only a reminder's due date, the start date that
  mirrored it, and the clock alarms anchored to the due time. It never edits
  titles, notes, lists, priority, or recurrence, never removes a recurring
  reminder's date, skips read-only lists, changes only selected reminders the
  list is currently showing, leaves a reminder alone when it changed after the
  list was loaded, and makes no network requests.

## Source And Runtime Ownership

- Source repository: the root of this Git checkout.
- Immutable runtime releases:
  `~/.local/share/icloud-reminders-google-sync/releases/<commit>/`.
- Stable runtime pointer: `~/.local/share/icloud-reminders-google-sync/current`.
- Config, credentials, state, and status:
  `~/.config/icloud-reminders-google-sync/`.
- LaunchAgent: `~/Library/LaunchAgents/com.icloud-reminders-google-sync.plist`.
- Date picker app: `~/Applications/미리알림 날짜.app` (bundle id
  `com.icloud-reminders-google-sync.due-picker`), installed only by
  `scripts/install-due-picker-app.sh`; the reviewed commit is recorded in the
  bundle's `Contents/Resources/release-source.txt`.
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

`scripts/install-due-picker-app.sh` runs the same gate before it builds, then
replaces `~/Applications/미리알림 날짜.app` in one rename, closing a running copy
first. It does not touch the runtime release, config, or LaunchAgent. Install
it only from reviewed main, and only when the user asked for the app.

## Structure

- `icloud_reminders_google_sync.py`: sync engine and CLI.
- `RemindersExport.swift`: reads Apple Reminders.
- `RemindersApply.swift`: applies changes back to Apple Reminders.
- `google-tasks-manager.command`: Finder/Terminal entrypoint for diagnosis,
  online Google checks, confirmed reconnect, safe state rebuild, LaunchAgent
  restart, and reviewed approval of a blocked bulk change. Recovery must back
  up private files before mutation and must keep deletion propagation disabled
  while rebuilding state.
- `setup-new-mac.sh`: verified release installer and first-sync flow.
- `make-migration-bundle.sh`: verified first-machine bundle helper.
- `scripts/check-release-source.sh`: fail-closed release source gate.
- `scripts/test-release-source-gate.sh`: isolated gate self-test.
- `test_icloud_reminders_google_sync.py`: local regression tests.
- `due-picker/`: the `미리알림 날짜` macOS app (SwiftUI + EventKit, built with
  the Command Line Tools only). `Sources/DueCore.swift` holds the pure date
  rules, Korean input parser, and alarm/start-date plans; `ReminderStore.swift`
  the EventKit reads and guarded writes; `Tools/` the icon, demo data, and
  offscreen snapshot renderer. SwiftUI's `@State` is a macro whose plugin ships
  only with Xcode, so view-local state lives in `ObservableObject`s.
- `scripts/build-due-picker-app.sh`: builds and signs the app bundle
  (`--snapshots DIR` renders the window with demo data instead).
- `scripts/install-due-picker-app.sh`: gated installer into `~/Applications`.
- `scripts/test-due-picker.sh`: date-rule tests (Linux and macOS) plus EventKit
  and app-model tests on unsaved in-memory reminders (macOS only).

## Required Source Checks

Run from this repository:

```sh
python3 -B -m unittest test_icloud_reminders_google_sync.py
python3 -B -m py_compile icloud_reminders_google_sync.py test_icloud_reminders_google_sync.py
bash -n setup-new-mac.sh make-migration-bundle.sh scripts/*.sh
bash scripts/test-release-source-gate.sh
bash scripts/test-due-picker.sh
```

For a change to the app's views, also run
`bash scripts/build-due-picker-app.sh` and render
`bash scripts/build-due-picker-app.sh --snapshots <dir>`; the snapshots use
demo data only. Never test the app's write path against real reminders.

Read-only runtime diagnosis is allowed when relevant:

```sh
/opt/homebrew/bin/python3 \
  ~/.local/share/icloud-reminders-google-sync/current/icloud_reminders_google_sync.py \
  doctor
```

Only run a live `sync` when the user explicitly asks to change Apple Reminders
or Google Tasks state.
