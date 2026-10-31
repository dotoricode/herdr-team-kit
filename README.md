# Herdr Team Kit

Herdr 안에서 제품별 팀장과 작업 워커를 조정하는 Python 표준 라이브러리 기반 도구입니다. 팀계정 우선 배정, 계정별 한도, 작업 소유권과 인계 기록을 로컬에서 관리합니다.

**현재는 운영 기반 구현입니다.** 가짜 provider·Herdr 경계와 로컬 Git/SQLite를 이용한 검사는 실행하지만 실제 Herdr·계정·모델 통합은 별도 검증이 필요합니다. 로그인, 컨텍스트 교체, PR 머지, 매뉴얼 빌드를 무인으로 완결하는 서비스는 아닙니다.

## 시작

Python 3.11 이상, Git, Herdr와 사용할 에이전트 CLI가 필요합니다. 별도 Python 패키지는 필요하지 않습니다.

```bash
python3 teamkit_cli.py --help
python3 -m unittest discover -s tests -v
```

`examples/config.example.json`을 저장소 밖의 개인 설정 디렉토리에 복사하고 실제 팀 루트·역할별 모델·프로필 경로·기대 계정을 지정합니다. 프로필마다 별도 로그인하며 인증정보는 설정에 넣지 않습니다. 지원 모델은 실제 계정에서 확인합니다.

```bash
python3 teamkit_cli.py --config /srv/example/private/config.json doctor
```

설정 기본 경로는 `~/.config/herdr-team-kit/config.json`, 상태 기본 경로는 `~/.local/state/herdr-team-kit/state.sqlite`입니다. 초기 한도 정상값의 유효기간은 120초이며 1–300초에서 설정할 수 있습니다. 이 값은 검증된 최적값이 아닌 보수적인 초기 설정입니다.

## 배정

Codex 한도는 모델 턴 없이 공식 App Server에서 읽습니다.

```bash
python3 teamkit_cli.py refresh --provider codex --account team
python3 teamkit_cli.py refresh --provider codex --account personal
python3 teamkit_cli.py route --provider codex
```

Claude는 `statusline --account team --display-command '기존 렌더러 명령'`으로 기존 출력을 유지하며 수집합니다. 실제 계정과 해당 프로필의 최신 응답 메타데이터를 대조하고 응답 시각에 고정해 redraw·resume이 TTL을 연장하지 않도록 합니다. 회사 Team 한도 제공은 가정하지 않습니다. 설정·되돌리기·관찰 시각의 한계는 [Claude 한도 수집](docs/statusline.md)을 확인하세요. `capture --provider claude`도 완전한 native 입력과 같은 응답 검증을 요구하며 `rate_limits`만 있는 입력은 승격하지 않습니다.

Herdr 관리 pane에서 작업을 등록하고 배정합니다.

```bash
python3 teamkit_cli.py create TASK-1 --team alpha --root /srv/example/team-alpha/repo --goal-file /srv/example/private/goal.txt
python3 teamkit_cli.py dispatch TASK-1
python3 teamkit_cli.py show TASK-1
```

팀장은 `create`에 `--role leader`를 지정합니다. 팀 루트가 Git 저장소가 아니면 설정과 명시한 지침 파일의 해시로 시작 기준을 기록합니다. 구현·리뷰 작업은 Git worktree가 필요합니다. 팀장은 수신 확인 후 `park TASK --owner OWNER`로 대기 상태에 들어가고, 총괄은 `tell TASK --message-file FILE`로 다음 요청을 전달합니다.

수신 에이전트는 배정 프롬프트의 `receipt` 명령으로 실제 프로필·계정을 확인한 뒤 작업합니다. `complete`는 검증 근거를 받아 `awaiting_review`까지만 이동합니다. PR 리뷰나 머지 성공을 대신하지 않습니다. 시간 초과는 `uncertain`으로 남으며 상태 확인 없이 재배정하지 않습니다.

운영 절차·인계·매뉴얼·승인 규칙은 `docs/operations.md`, 검증 범위와 남은 작업은 `docs/verification.md`를 확인하세요.

## 공개 자료 경계

이 저장소에는 범용 코드와 가짜 설정만 둡니다. 회사 제품 코드·지침·매뉴얼·실행 기록·계정 정보는 별도 로컬 설정으로 연결합니다. 공개 검사 스크립트는 알려진 실수를 찾는 보조 수단이며 비밀정보 검토를 대신하지 않습니다.
