# Contributing

Thank you for helping improve Apple Reminders ↔ Google Tasks Sync.

## Before opening an issue

- Search existing issues first.
- Reproduce the problem with the latest `main`.
- Remove reminder titles, notes, account identifiers, email addresses, local
  paths, OAuth responses, tokens, and exported data from screenshots or logs.
- Use GitHub's private vulnerability reporting flow for security issues.

## Development

Create a focused branch and run the source-only checks:

```bash
python3 -B -m unittest test_icloud_reminders_google_sync.py
python3 -B -m py_compile \
  icloud_reminders_google_sync.py \
  test_icloud_reminders_google_sync.py
bash -n setup-new-mac.sh make-migration-bundle.sh scripts/*.sh
bash scripts/test-release-source-gate.sh
```

Tests must not read or modify live Apple Reminders, Google Tasks, OAuth
credentials, or the user's LaunchAgent. Use synthetic fixtures with
`example.invalid` addresses and clearly fake identifiers.

## Pull requests

- Keep one behavioral change per pull request.
- Add a regression test for sync behavior or safety changes.
- Describe runtime and installation impact.
- Do not weaken deletion guards, account binding, release-source verification,
  credential isolation, or log redaction without an explicit security reason.
- Confirm that no credentials, sync state, logs, or user data are included.

By contributing, you agree that your contribution is licensed under the MIT
License.
