# 설계 승인과 작업 현황

제품 작업은 `work`에 한 번 등록하고, 실행·재시도·새 세션은 `create --work`로 연결합니다. 작업 건수와 모델 실행 횟수를 분리합니다. 현재 대화의 사용자 승인을 해당 설계 버전에 기록하기 전에는 구현을 배정하지 않습니다. `plan`은 모델 라우팅 미리보기이고, `authorize`는 모델 선택 승인입니다. 제품 설계 승인과 혼용하지 않습니다.

## 비공개 설계

`private_plan_root`의 기본값은 `~/.local/state/herdr-team-kit/private-plans`입니다. 모든 Git 저장소와 인증 홈 밖이어야 합니다. Git 저장소에 들어 있는 원본 문서도 입력으로 받지 않습니다. 디렉터리는 700, 새 문서는 600으로 만들고 제품 저장소에 복사하거나 링크하지 않습니다. Git에 편입되거나 경로가 바뀌면 새 실행을 거절합니다. 이 검사는 누군가 내용을 다른 파일로 복사해 게시하는 행위까지 막는 인증 시스템은 아닙니다. 커밋 전 실제 diff·추적 파일 검토도 유지합니다.

업무 폴더의 `r1/plan.md`에는 사람이 검토할 설계를, `r1/contract.json`에는 다음 계약을 둡니다. 제품 자료는 공개 예제에 넣지 않습니다.

```json
{
  "title": "One bounded feature",
  "requirements": ["Preserve result and failure semantics"],
  "allowed_paths": ["src/feature.c", "tests/feature/"],
  "steps": [{"id": "behavior", "title": "Verify approved behavior", "weight": 100}]
}
```

배정 단위와 메모리 소유권·수명·실패 조건을 먼저 정합니다. 파일·클래스 분리는 구체적인 책임 경계가 필요할 때만 사용하고, 중복 규칙과 주변 동작 변경을 줄입니다. 사용자 선택에는 결정 대상·필요 이유·선택지의 작동 방식·장단점·기술적 실패 조건·검증을 포함합니다. 전문용어는 그대로 쓰되 뜻을 쉬운 문장으로 설명합니다.

## 등록과 승인

```bash
python3 teamkit_cli.py work create WORK-1 --team alpha --root /srv/example/team-alpha/repo --plan-file /srv/example/private/plan.md --contract-file /srv/example/private/contract.json
python3 teamkit_cli.py work submit WORK-1
python3 teamkit_cli.py work show WORK-1
python3 teamkit_cli.py work approve WORK-1 --user-approved --approval-reference 'User message selecting revision 1'
python3 teamkit_cli.py create TASK-1 --team alpha --root /srv/example/team-alpha/repo --work WORK-1 --goal-file /srv/example/private/goal.txt
```

approve는 사용자와 검토한 깨끗한 작업 트리의 기준 SHA, 버전·설계·계약의 해시를 저장합니다. `--user-approved`와 reference는 운영자가 확인한 승인 기록이며 사용자 인증을 대신하지 않습니다. 모델은 자기 설계를 승인하지 않습니다. 승인 파일을 바꾸면 기존 승인이 무효가 됩니다. `work revise`는 이전 문서를 보존한 새 버전을 만들고 승인을 다시 기다립니다. 같은 내용의 새 버전도 이전 실행에 자동 적용하지 않습니다.

`require_design_approval: true`이면 연결되지 않은 구현·리뷰 배정을 거절합니다. 명시적으로 work에 연결한 작업은 설정값과 관계없이 승인 검사를 적용합니다. 배정 전과 SQLite claim 트랜잭션 안에서 재검사하고, 승인 경로 밖 변경은 거절합니다. 경로·버전 검사는 의미상 설계 준수를 증명하지 않으므로 독립 리뷰가 원래 계약과 실제 소비 경로를 대조해야 합니다.

설계 배정은 `--role leader --work-kind design --work WORK-1`로 분류합니다. 설계 route의 high는 상태 관리용 low와 별개입니다. 새 Codex 설계 세션의 cwd는 제품 소스가 아닌 비공개 설계 폴더이며, 임시 디렉터리의 기본 쓰기 범위를 제외합니다. 실제 sandbox와 승인 경계를 별도 검증하고 제품 쓰기를 위한 승인을 요청하지 않습니다.

## 여러 날의 작업과 검증

작업 ID·승인·실패·실행 기록은 SQLite에 남고 날짜가 바뀌어도 만료시키지 않습니다. 단, 계정 한도는 기존 유효기간을 적용합니다. 실행 중·결과 불명인 주체가 있으면 인계와 정지 확인부터 하며 새 버전·재개 기록을 만들지 않습니다. 자동 종료나 PR 머지를 추가하는 기능은 아닙니다.

```bash
python3 teamkit_cli.py work checkpoint WORK-1 --evidence-file /srv/example/private/checkpoint.json
python3 teamkit_cli.py work record WORK-1 --step behavior --evidence-file /srv/example/private/evidence.json
python3 teamkit_cli.py work finish WORK-1 --review-task REVIEW-1
```

checkpoint 입력에는 비어 있지 않은 `verification` 목록과 `next`를 넣습니다. 실제 HEAD와 index·working tree·untracked 내용의 지문을 기록하고 다음 구현 claim에서 확인한 뒤 소비합니다. 강제 종료의 결과를 모르면 inspect하며 파일을 덮어쓰거나 명령을 반복하지 않습니다. 변경된 입력만 재검증하고, 같은 SHA·입력·환경의 유효한 근거는 재사용합니다.

record는 승인된 단계의 완료 근거와 파일 해시·소스 지문을 남깁니다. 기록 자체가 명령 실행이나 의미상 검증을 대신하지 않습니다. 진행률은 승인한 단계 가중치(합계100)의 합이며 마지막 독립 리뷰 전에는 최대99입니다. finish는 전체 단계와 동일 승인 버전·현재 clean SHA의 별도 reviewer 작업, 명시적 `review_verdict: "pass"`를 요구합니다. `findings`·`blocked` 리뷰는 완료로 처리하지 않습니다. push·게시·merge는 별도 승인 범위입니다.

## 모델을 깨우지 않는 현황

```bash
python3 teamkit_cli.py dashboard --once --json
python3 teamkit_cli.py dashboard --publish
```

TUI는 모델이 아닌 일반 프로그램입니다. read-only SQLite snapshot을 수초마다 읽고 요청 모델/effort·업무 단계·승인 대기·갱신 경과를 표시합니다. native idle/done을 업무 완료로 바꾸지 않습니다. 설계 승인 전 진행률은 미정이며 완료 단계 수를 따로 표시합니다. 기록 갱신 경과는 마지막 업무 기록 시각으로, 프로세스 생존 여부가 아닙니다. 화면을 닫아도 업무 기록과 AI 세션은 유지됩니다.

`dashboard_workspaces`는 `{ "alpha": { "id": "w1", "label": "Alpha leader" } }`처럼 명시합니다. `herdr_session`을 지정하면 존재하는 세션인지 확인하고, 이름까지 일치하는 workspace에만 요약 metadata를 전달합니다. publisher는 하나만 실행하며 10초 간격·30초 TTL로 표시를 갱신합니다. metadata는 서버 재시작 후 복원되지 않으므로 dashboard가 장부에서 재구성합니다. Herdr의 원래 agent lifecycle은 바꾸지 않습니다.

좌측 rows에는 `$work_summary`, `$approval_wait`, `$work_blocked`를 넣을 수 있습니다. 빨강=확인 필요, 노랑=승인·대기, 파랑=진행, 초록=검증 완료로 색과 상태 이름을 함께 사용합니다. 설정 변경 전 백업하고 실제 화면을 확인하며 기존 밝은 배경을 유지합니다.

전체 대화·로그·설계를 상태 갱신마다 모델에 보내지 않습니다. 작업에 필요한 절과 변경분만 읽습니다. 캐시 읽기·입력·출력·요청·재시도는 별도 측정하고, 관측하지 못한 비용과 절감률은 미측정으로 남깁니다.
