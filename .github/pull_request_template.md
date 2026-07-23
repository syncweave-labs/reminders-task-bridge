## Scope and result

- Scope:
- Result:

## Verification

- [ ] `python3 -B -m unittest test_icloud_reminders_google_sync.py`
- [ ] `bash -n setup-new-mac.sh make-migration-bundle.sh scripts/*.sh`
- [ ] GitHub Actions CI is green.

## Source and runtime safety

- [ ] No credentials, OAuth tokens, sync state, exported task/reminder data, or logs are included.
- [ ] Runtime or LaunchAgent impact is described below, or this change has none.

Runtime/install impact:

Rollback:
