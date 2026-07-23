# iCloud Reminders ↔ Google Tasks 동기화

Apple Reminders와 Google Tasks를 로컬 Mac에서 양방향 동기화하는 도구다.
Google은 iCloud Reminders를 직접 읽고 쓸 수 없으므로, 로그인된 Mac의
EventKit과 사용자 LaunchAgent가 연결 지점이 된다.

This is a local-first, bidirectional bridge between Apple Reminders and Google
Tasks. It runs in the signed-in macOS user session, keeps credentials and sync
state outside the repository, and blocks unexpectedly destructive changes
before writing them.

## Project status

The project is maintained for real-world use on macOS and welcomes focused bug
reports and pull requests. It is intentionally conservative: correctness,
account binding, deletion safety, and recoverability take priority over adding
new integrations.

- License: [MIT](LICENSE)
- Contribution guide: [CONTRIBUTING.md](CONTRIBUTING.md)
- Code of conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- Security policy: [SECURITY.md](SECURITY.md)
- Supported source branch: `main`

## Quick start

Requirements:

- a Mac signed in to iCloud with Reminders enabled
- Python 3 and the Swift toolchain from Xcode Command Line Tools
- a Google Cloud project with the Google Tasks API enabled
- an OAuth Desktop client JSON created for your own Google Cloud project

Clone the reviewed source:

```bash
git clone https://github.com/syncweave-labs/reminders-task-bridge.git \
  ~/apps/icloud-reminders-google-sync
cd ~/apps/icloud-reminders-google-sync
```

Run the source-only checks before installation:

```bash
python3 -B -m unittest test_icloud_reminders_google_sync.py
python3 -B -m py_compile \
  icloud_reminders_google_sync.py \
  test_icloud_reminders_google_sync.py
bash -n setup-new-mac.sh make-migration-bundle.sh scripts/*.sh
bash scripts/test-release-source-gate.sh
```

Installation changes local credentials, runtime files, Apple Reminders access,
Google authentication, and a user LaunchAgent. Read the verified installation
section below before running `setup-new-mac.sh`. Pass your own Google Cloud
project explicitly:

```bash
GCP_PROJECT='<your-google-cloud-project-id>' \
DEPLOY_EXPECTED_COMMIT="$(git rev-parse HEAD)" \
bash setup-new-mac.sh
```

## Privacy and security

OAuth credentials, refresh tokens, task/reminder content, synchronization
state, status files, logs, and exports are local runtime data and must never be
committed or pasted into public issues. The default `doctor` command is
local-only and redacts stored errors and absolute paths. Use
`doctor --online` only when you intentionally allow a Google token refresh and
an API connectivity check.

Security reports should follow [SECURITY.md](SECURITY.md), not a public issue.

## 운영 계약

- Apple Reminders 목록과 같은 이름의 Google Tasks 목록을 사용한다.
- 빈 목록과 날짜 없는 항목을 포함해 양방향으로 동기화한다.
- Apple 완료는 Google 완료로, Apple 삭제는 Google 삭제로 반영한다.
- 양쪽이 바뀌면 더 최근 수정 내용을 기준으로 맞춘다.
- 동기화 후 Google Tasks를 다시 읽어 제목과 마감일을 검증한다.
- Google 목록 조회가 늦을 때는 최근 task 단건 조회와 짧은 재시도를
  사용한다.
- 동시 실행은 잠금 파일로 막고, 마지막 결과는 `status.json`에 쓴다.
- 사용자 세션의 LaunchAgent가 1분 간격으로 실행한다. Mac이 잠자거나
  사용자가 로그아웃한 동안 실행되는 서버 데몬은 아니다.
- 새 Mac의 첫 동기화는 반드시 `--no-delete-stale`로 상태 맵을 재구축한다.

## 목록별 동기화 정책

`target_service: "tasks"`에서는 Apple Reminders 목록 제목을 키로 사용해
목록마다 방향, 삭제·완료 전파, 날짜 없는 항목, 충돌 처리를 다르게 설정할
수 있다. 지정하지 않은 필드는 기존 전역 설정을 그대로 상속하므로 기존
config는 변경 없이 같은 방식으로 동작한다.

```json
{
  "bidirectional": true,
  "delete_stale": true,
  "tasks_sync_undated": true,
  "conflict_policy": "newer_wins",
  "list_policies": {
    "업무": {
      "direction": "bidirectional",
      "delete_propagation": true,
      "sync_undated": true,
      "conflict_policy": "newer_wins"
    },
    "보관": {
      "direction": "apple_to_google",
      "delete_propagation": false,
      "sync_undated": false,
      "conflict_policy": "skip"
    },
    "Google 수신함": {
      "direction": "google_to_apple",
      "delete_propagation": false,
      "sync_undated": true,
      "conflict_policy": "skip"
    }
  }
}
```

- `direction`: `apple_to_google`, `google_to_apple`, `bidirectional` 중 하나다.
  전역 기본값은 `bidirectional: true`이면 `bidirectional`, 아니면
  `apple_to_google`이다.
- `delete_propagation`: Apple 삭제·완료를 Google에 반영하고 Google
  삭제·완료를 Apple에 반영할지 결정한다. 중복 Google 항목 정리도 이
  정책을 따른다. 전역 기본값은 `delete_stale`이다.
- `sync_undated`: 날짜 없는 활성 Apple 항목을 내보내고, 날짜 없는 신규
  Google 항목을 가져올지 결정한다. 전역 기본값은
  `tasks_sync_undated`이다.
- `conflict_policy`: 양쪽 내용이 모두 바뀐 경우 `skip` 또는
  `newer_wins`를 사용한다. 전역 기본값은 `conflict_policy`다. 이 선택은
  `bidirectional` 목록에서만 필요하며, 단방향 목록은 지정된 출발 쪽을
  source of truth로 사용한다.

목록 제목은 Reminders에 표시되는 이름과 정확히 같아야 한다. 알 수 없는
필드, 잘못된 enum, 문자열로 쓴 boolean 등은 인증이나 동기화 쓰기 전에
config 오류로 중단한다. `--no-delete-stale`은 목록별
`delete_propagation: true`보다 우선하는 전체 안전 차단이다.

빈 Apple 목록 미러링은 계속 유지된다. 다만 `google_to_apple` 전용 목록과
같은 이름의 Google Tasks 목록이 없으면, 방향 정책을 어기며 새 Google
목록을 만들지 않고 해당 주기를 건너뛴다. Google에만 있는 일반 task를
처음 가져오려면 기존과 같이 `tasks_import_unsynced: true`가 필요하다.
최초 연결 시 양쪽 항목을 추적하기 위한 동기화 metadata가 Google task의
notes에 추가될 수 있지만 제목·날짜·완료 상태의 흐름은 `direction`을
따른다. 목록별 정책은 Google Tasks 모드에 적용되며 Google Calendar
모드는 기존 전역 정책을 사용한다.

## 프로젝트와 운영 경로

소스와 실행 중인 릴리스를 분리한다.

```text
~/apps/icloud-reminders-google-sync/                  # Git 소스 저장소
~/.local/share/icloud-reminders-google-sync/
  releases/<full-commit-sha>/                         # 불변 실행 릴리스
  current -> releases/<full-commit-sha>               # LaunchAgent의 고정 경로
~/.config/icloud-reminders-google-sync/               # config, credentials, state, status
~/Library/LaunchAgents/com.icloud-reminders-google-sync.plist
/tmp/icloud-reminders-google-sync.out.log
/tmp/icloud-reminders-google-sync.err.log
```

설치기는 두 로그를 사용자 전용 `0600`으로 만들고 기본 5 MiB 이상이면
기존 내용을 `.1`로 한 번 회전한 뒤 LaunchAgent를 다시 시작합니다.
`ICLOUD_SYNC_LOG_MAX_BYTES`로 재설치 시 회전 기준을 조정할 수 있습니다.

LaunchAgent는 소스 체크아웃이나 Codex worktree를 직접 실행하지 않는다.
항상 `~/.local/share/icloud-reminders-google-sync/current`를 실행하므로 작업
브랜치나 임시 폴더를 정리해도 운영 경로가 사라지지 않는다.

## 소스 변경과 CI

기능 변경은 `codex/<task>` 브랜치에서 하고 pull request로 `main`에
병합한다. GitHub Actions와 로컬 기본 검사는 같다.

```bash
python3 -B -m unittest test_icloud_reminders_google_sync.py
python3 -B -m py_compile icloud_reminders_google_sync.py test_icloud_reminders_google_sync.py
bash -n setup-new-mac.sh make-migration-bundle.sh scripts/*.sh
bash scripts/test-release-source-gate.sh
```

실제 Apple/Google 데이터를 읽거나 쓰지 않아도 위 검사를 실행할 수 있다.

## 검증된 릴리스 설치

`setup-new-mac.sh`는 runtime/config/LaunchAgent를 바꾸기 전에
`scripts/check-release-source.sh`를 실행한다. 기본 설치는 다음 조건을
모두 만족해야 한다.

- 현재 브랜치가 `main`
- tracked/untracked 변경이 없는 깨끗한 worktree
- `origin`이 `syncweave-labs/reminders-task-bridge`
- `HEAD == origin/main == GitHub의 live main`
- `HEAD == DEPLOY_EXPECTED_COMMIT`

검토·병합된 main을 설치하는 명령:

```bash
cd ~/apps/icloud-reminders-google-sync
git switch main
git pull --ff-only
DEPLOY_EXPECTED_COMMIT="$(git rev-parse HEAD)" bash setup-new-mac.sh
```

설치 스크립트는 다음 순서로 동작한다.

1. 릴리스 소스 게이트를 통과한다.
2. 검증한 commit의 실행 파일을 불변 `releases/<commit>/`에 복사한다.
3. `current` 심볼릭 링크를 해당 릴리스로 전환한다.
4. config와 OAuth client 위치를 준비하고 Google 인증을 확인한다.
5. Apple Reminders 접근을 확인한다.
6. `--no-delete-stale` dry-run과 첫 동기화를 실행한다.
7. stable `current` 경로를 사용하는 LaunchAgent를 쓰고 로드한다.

`DEPLOY_GIT_ALLOW_DIRTY`, `DEPLOY_GIT_ALLOW_NON_MAIN`,
`DEPLOY_GIT_ALLOW_UNPUSHED`는 비상 복구용이다. 하나라도 사용하면
`DEPLOY_GIT_OVERRIDE_REASON`이 필수이며, 경고에 사유가 기록된다. 기대
GitHub origin은 우회할 수 없다. 정상 설치에서는 이 override를 쓰지 않는다.

## 새 Mac용 검증 번들

기존 Mac의 clean main에서 번들을 만든다.

```bash
cd ~/apps/icloud-reminders-google-sync
git switch main
git pull --ff-only
DEPLOY_EXPECTED_COMMIT="$(git rev-parse HEAD)" bash make-migration-bundle.sh
```

기본 출력은 다음과 같다.

```text
~/Desktop/icloud-reminders-google-sync-migration-YYYYMMDD-HHMMSS.tar.gz
```

번들은 실행 코드, `release-source.txt`, `release-source.sha256`, 설치 안내를
포함한다. 기본값은 OAuth desktop client `credentials.json`도 포함할 수
있으므로 개인 보안 파일로 취급한다. ADC 로그인, refresh token,
`state.json`, 로그, 내보낸 데이터는 포함하지 않는다.

새 Mac에서는 다음 순서로 진행한다.

1. 같은 iCloud 계정으로 로그인하고 Reminders 동기화를 켠다.
2. 번들을 개인 전송 수단으로 옮겨 `~/apps/icloud-reminders-google-sync`에
   푼다.
3. `NEW_MAC_SETUP.txt`에 생성된 commit과 명령을 그대로 실행한다.
4. Google 로그인과 macOS Reminders 권한 승인을 완료한다.

번들 설치는 Git metadata가 없으므로 명시적인
`DEPLOY_GIT_ALLOW_BOOTSTRAP=1`과 audit reason을 요구한다. 이 모드는
manifest의 저장소/branch/commit 및 SHA-256 파일 검증을 통과해야만
runtime을 변경한다. 임의 압축 폴더를 설치하는 일반 우회 수단이 아니다.

## 상태 확인

소스 테스트와 실제 사용자 세션 상태는 별개다. 운영 확인에는 아래
read-only 요약을 사용한다.

```bash
/opt/homebrew/bin/python3 \
  ~/.local/share/icloud-reminders-google-sync/current/icloud_reminders_google_sync.py \
  doctor
```

설치가 끝난 뒤에는 Finder의 사용자 `응용 프로그램` 폴더에 생성되는
`Google Tasks 동기화 관리.command`를 여는 것이 기본 관리 경로다. Script
Editor에서 진단 문구만 확인하거나 긴 Terminal 명령을 복사할 필요 없이 한
메뉴에서 다음 작업을 선택할 수 있다.

1. 현재 상태 보기: 로컬 파일과 LaunchAgent, 마지막 성공 시각, 연속 실패
   횟수만 읽는다. Google 요청이나 token 갱신은 하지 않는다.
2. Google 연결 실제 확인: token을 갱신할 수 있으며 Tasks API와 목록 조회가
   실제로 되는지 확인한다. OpenID UserInfo 권한이 있으면 현재 Google 계정의
   이메일을 이 로컬 화면에만 표시하며 state/status에는 저장하지 않는다.
3. Google 연결 복구 및 안전 재연결: private config/state/token을 먼저
   `~/.config/icloud-reminders-google-sync/backups/`에 `0600`으로 백업하고
   LaunchAgent를 잠시 멈춘다. 인증이 실제로 만료된 경우에만 브라우저
   로그인을 시작한다.
4. 백그라운드 다시 시작: 인증 만료, 계정 binding 차이, 대량 변경 차단이
   없을 때만 LaunchAgent를 다시 올린다.

관리 메뉴는 상태를 다음처럼 구분한다.

| 판정 | 의미 | 관리 메뉴의 조치 |
| --- | --- | --- |
| `healthy` | 최근 동기화가 성공했고 LaunchAgent가 실행 중 | 조치 없음 |
| `auth_required` | Google token이 없거나 만료·취소됨 | 브라우저 로그인 후 Tasks API 재검증 |
| `account_binding_required` | Google 인증은 가능하지만 Apple/Google 계정 기준이 저장 상태와 다름 | 계정 확인 후 기존 상태를 백업으로 이동하고 안전 재구축 |
| `mutation_blocked` | 완료·삭제 계획이 안전 한도를 넘음 | 자동 승인하지 않고 별도 dry-run 검토 |
| `agent_stopped` | 백그라운드 작업이 로드되지 않음 | 원인을 해결한 뒤 메뉴에서 재시작 |

`account_binding_required` 복구는 Google/Apple 데이터를 지우지 않는다. 먼저
기존 state를 별도 백업으로 옮기고 `--no-delete-stale` dry-run을 보여 준다.
사용자가 의도한 계정 조합과 계획을 각각 확인한 뒤에만 같은 안전 옵션으로
새 state map을 저장하고 LaunchAgent를 다시 시작한다. 인증 자체가 정상인
경우에는 불필요한 Google 로그인을 반복하지 않는다.

Terminal에서 같은 관리 기능을 직접 열 수도 있다.

```bash
RUNTIME="$HOME/.local/share/icloud-reminders-google-sync/current"
/opt/homebrew/bin/python3 "$RUNTIME/icloud_reminders_google_sync.py" manage menu
```

기본 `doctor`는 credential을 읽어 Google에 보내거나 token을 갱신하지
않는다. config·helper·stable runtime·LaunchAgent 설치/로드 여부와 최신
`status.json`의 상태·시각·연속 실패 횟수만 요약한다. 저장된 원문 오류와
절대 경로는 출력하지 않고 상태별 다음 조치를 안내한다.

Google 인증 refresh와 Tasks API까지 확인해야 하고 local OAuth token 갱신을
허용할 때만 `doctor --online`을 명시한다. Google 응답 body는 이 경우에도
터미널에 출력하지 않는다.

누적 stderr의 오래된 오류만으로 현재 실패라고 판단하지 않는다. 자세한
원문이 꼭 필요하면 private log와 `status.json`을 Mac 안에서 직접 확인하고,
credential·token·로컬 경로가 포함될 수 있으므로 채팅이나 issue에 그대로
붙여넣지 않는다. 현재 상태 판단은 `state`, `last_success_at`,
`consecutive_failures`와 doctor가 확인한 launchd 상태를 우선한다.

LaunchAgent가 재시작되어도 실패 횟수는 마지막 상태에서 이어지고, 예상하지
못한 브라우저 OAuth 실행 오류도 루프를 종료하지 않고 `auth_required`로
기록한다. 자동 OAuth 실패는 timeout, 브라우저 실행 오류, 사용자 거부를
구분하지 않고 최소 5분부터 실패 횟수에 따라 지수 백오프하며, 설정된
`auto_reauth_min_interval_seconds`를 상한으로 사용한다. 따라서 60초 scheduler가
로그인 창을 매 주기 다시 열지 않는다. 사용자가 직접 실행하는 `auth` 명령은
이 자동 재시도 제한과 분리된다. 다른 수동 동기화가 잠금을 잡고 있으면
해당 주기는 조용히 건너뛰며, 실제 실행 중인 작업의 `running` 상태를
덮어쓰지 않는다.

`state.json`은 선택된 Apple Reminders account source와 활성 Google OAuth
credential에 opaque hash로 바인딩된다. account source나 OAuth credential이
바뀌면 이전 task/event mapping, conflict state, cursor를 새 계정에 재사용하지
않고 원격 목록 조회·변경 전에 `account_binding_required`로 중단한다. 기존
항목이 있는 legacy state에 binding이 없을 때도 자동으로 현재 계정을
신뢰하지 않는다. 이 경우 동기화를 계속 재시도하지 말고 private state를
백업한 뒤, 의도한 두 계정을 확인하고 기존 state를 별도로 보존한 상태에서
`--no-delete-stale`로 새 state를 명시적으로 재구축한다. account identifier와
refresh token 원문은 state, status, doctor 출력에 저장하지 않는다.

## 수동 동기화

아래 명령은 Apple Reminders 또는 Google Tasks를 변경할 수 있다. 실제
변경을 원할 때만 실행한다.

```bash
RUNTIME="$HOME/.local/share/icloud-reminders-google-sync/current"
/opt/homebrew/bin/python3 "$RUNTIME/icloud_reminders_google_sync.py" sync --dry-run
/opt/homebrew/bin/python3 "$RUNTIME/icloud_reminders_google_sync.py" sync
```

모든 동기화는 Apple/Google 쓰기 전에 양방향 mutation plan을 완성하고
fingerprint를 출력한다. 기본값은 파괴적 완료/삭제/중복 정리가 25건을
초과하거나 현재 관리 항목의 25%를 초과하면 쓰기 전에 중단하는 것이다.
`max_destructive_changes`, `max_destructive_ratio`,
`destructive_approval_ttl_seconds`로 기준을 조정할 수 있다.

의도한 대량 변경은 먼저 dry-run에서 fingerprint와 짧게 유효한 승인 token을
확인한 뒤, 같은 plan에만 적용되는 token을 명시적으로 전달한다.

```bash
/opt/homebrew/bin/python3 "$RUNTIME/icloud_reminders_google_sync.py" sync --dry-run
/opt/homebrew/bin/python3 "$RUNTIME/icloud_reminders_google_sync.py" sync \
  --approve-mutation-plan '<fingerprint>:<issued-unix-time>'
```

plan이 달라졌거나 token이 만료되면 다시 차단된다. `status.json`에는 원문
할 일 제목이나 ID 대신 fingerprint, 종류별 개수, 비율, 차단 코드만 기록한다.

새 상태 맵을 만들 때는 삭제/완료 전파를 막는다.

```bash
RUNTIME="$HOME/.local/share/icloud-reminders-google-sync/current"
/opt/homebrew/bin/python3 "$RUNTIME/icloud_reminders_google_sync.py" sync --dry-run --no-delete-stale
/opt/homebrew/bin/python3 "$RUNTIME/icloud_reminders_google_sync.py" sync --no-delete-stale
```

`--no-delete-stale`에서는 양방향 삭제와 중복 삭제도 실행하지 않으므로 새
Mac의 상태 맵을 먼저 안전하게 재구축할 수 있다.

## 자동 실행과 롤백

LaunchAgent를 내리거나 다시 올리는 명령은 사용자 세션의 live operation이다.

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.icloud-reminders-google-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.icloud-reminders-google-sync.plist
```

새 릴리스에 문제가 있으면 `releases/`에서 이전 검증 commit을 고른 뒤
`current`를 되돌리고 LaunchAgent를 재시작한다.

```bash
ls -1 ~/.local/share/icloud-reminders-google-sync/releases
ln -sfn "releases/<previous-full-commit-sha>" \
  ~/.local/share/icloud-reminders-google-sync/current
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.icloud-reminders-google-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.icloud-reminders-google-sync.plist
```

롤백 후 `launchctl print`, `doctor`, 최신 `status.json`으로 복구를 확인한다.

## 제한과 문제 해결

- Google Tasks API는 날짜 `due`를 지원하지만 시간과 반복 규칙을 그대로
  저장하지 않는다. 반복 규칙의 원본은 Apple Reminders다.
- Google refresh token이 만료되거나 철회되면 사용자 계정 선택, 2FA,
  OAuth 승인이 다시 필요할 수 있다.
- `credentials.json`은 source control에 넣지 않는다. 설치할 때만 소스
  옆에 임시로 두거나
  `~/.config/icloud-reminders-google-sync/credentials.json`에 둔다.
- Reminders 접근 오류는 stable runtime helper로 확인한다.

```bash
swift ~/.local/share/icloud-reminders-google-sync/current/RemindersExport.swift --lists-only
```

- 목록이 0개라면 Reminders 앱에서 iCloud 목록이 내려왔는지와
  `System Settings → Privacy & Security → Reminders` 권한을 먼저 확인한다.
- `Google Tasks title/due consistency check failed`는 최근 task 단건 조회와
  설정된 재시도 후에도 제목/날짜가 다를 때만 최종 오류로 남는다.
