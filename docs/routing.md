# 작업별 모델·effort 배정

역할 기본값과 계정 선택을 유지하면서 명시적으로 분류한 작업에 모델·effort를 선택합니다. 설정의 선택적 `routing` 항목은 `general`, `small`, `design`, `wording` 각각의 provider·model·effort를 지정합니다. 모델 이름은 운영 환경에서 지원을 확인한 고정 ID를 사용합니다. 설정 예제는 가짜 모델 이름이며 그대로 실행할 수 없습니다.

`create --work-kind small`은 구현 워커의 명확한 작은 작업, `--work-kind design`은 팀장의 설계·조정, `--work-kind wording`은 리뷰어의 명확한 문구 검토입니다. 기본값 `general`은 기존 역할 설정을 유지하되 구현 역할은 `routing.general`이 있으면 사용합니다. `--risk security`, `jni`, `authentication`, `concurrency`는 반복 지정할 수 있습니다. 위험이 표기된 small/wording은 general로 처리합니다. 분류는 작업 계약에 저장되며 같은 작업 ID로 몰래 바꿀 수 없습니다. 자연어 목표를 자동 분류하지 않으므로 총괄이 위험 표기를 누락하지 않아야 합니다.

```bash
python3 teamkit_cli.py create TASK-1 --team alpha --root /srv/example/team-alpha --goal-file /srv/example/private/goal.txt --work-kind small
python3 teamkit_cli.py plan TASK-1
```

`plan`은 모델 배정을 미리 보여주며 모델 호출·소유권 claim은 하지 않습니다. 필요한 routing 항목이 없으면 임의 모델을 고르지 않고 거절합니다. dispatch는 같은 결정을 실제 native 실행 인자와 장부의 요청 모델·effort에 사용합니다. 실제 적용값은 실행 화면·응답 근거로 별도 대조해야 합니다.

실패 후 인계할 때 기존 evidence JSON에 다음 항목을 추가합니다. 원인 식별자는 같은 원인에 동일하게 부여하고 비밀값을 넣지 않습니다. `verification`에는 실제 실패 근거 위치·결과를, `next`에는 다음 행동을 적습니다. 실제 실행자가 멈춘 것을 확인한 뒤 기존 handoff 명령의 `--writer-stopped`를 사용합니다.

```json
{"verification":["fixture assertion failed; private evidence location recorded locally"],"next":"investigate the same root cause","failure":{"cause":"same-root-cause","kind":"reasoning"}}
```

실패 종류는 `reasoning`, `insufficient_evidence`, `environment`, `typo`입니다. 작업 기록에 실패 당시 effort와 원인·종류가 누적됩니다. reasoning의 같은 원인 2회 또는 insufficient_evidence는 다음 배정을 high로 올립니다. 환경 오류와 오타는 상향 횟수에서 제외합니다. 마지막 유효 실패가 high 이상에서 발생했다면 `ask_user`를 반환하고 인증 조회·claim·전송을 하지 않습니다. 그 이후 모델 선택은 사용자 판단이며 자동 최고 effort·다른 계열 모델 전환은 없습니다.

계정은 선택한 provider 안에서 회사 구독 → 개인 구독 → 대기 순서입니다. 두 한도 창이 유효하고 90% 미만이어야 합니다. 한도 누락·만료·오류를 0%로 바꾸거나 다른 provider·API·추가 결제로 우회하지 않습니다. 작업 분류와 실패 기록은 운영자의 입력이며 자동 진단·실제 모델 지원·전체 제품 검증을 대신하지 않습니다.

사용자 결정을 받은 뒤에만 다음 형식의 로컬 파일을 작성합니다. `failure_count`는 현재 작업의 전체 failures 길이에 고정하고, 승인 근거는 로컬 기록 위치나 실제 사용자 답변 식별자로 남깁니다.

```json
{"failure_count":2,"approval_reference":"local-user-answer-1","provider":"codex","model":"replace-with-user-selected-model","effort":"high"}
```

```bash
python3 teamkit_cli.py authorize TASK-1 --decision-file /srv/example/private/decision.json --user-approved
```

이 명령은 운영자가 이미 받은 승인을 기록하는 수단이며 승인을 생성하거나 확인하는 인증 체계가 아닙니다. 자동 워커가 사용자 결정을 만들어서는 안 됩니다. 소유자가 없는 handed_off 상태의 high 실패와 현재 실패 개수가 일치해야 승인 기록을 저장합니다. 기존 작업 ID·실패 이력은 보존하며, 사용자가 선택한 provider·model은 다음 명시적 결정 전까지 유지합니다. 상향 횟수는 결정 이후 실패부터 다시 세며, high 이상에서 다시 미해결이면 새로운 승인이 필요합니다. 이전 failure_count의 승인 파일을 그대로 재적용할 수 없습니다. 모델 변경은 이 명시적 결정에 한해서 가능하며 provider는 구성된 구독 프로필만 사용합니다. 계정과 한도·권한 검사는 이후 dispatch에서 그대로 수행합니다.
