# 작업 카드 생성·관리 지침

카드 등록·상태 변경·결정 질문 작성·답변 처리·인계 시 읽습니다. 한 업무는 하나의 work ID로 유지하고, 실행 세션과 설계 버전은 그 업무에 연결합니다. 이 지침을 읽은 사실이 업무 실행이나 승인 권한을 추가하지는 않습니다.

## 1. 현재 카드 확인

App 전용 실행기에서 Python·설치 루트·config·state·bindings 경로를 확인합니다. 제품 저장소의 cwd나 시스템 Python을 그대로 가정하지 않고 의존성이 설치된 실행 환경을 사용합니다. 기본 TUI 경로와 섞지 않습니다. 아래 예시는 가상 경로이며 실제 값으로 바꾼 뒤 사용합니다.

```sh
PYTHON=/srv/example/ui-venv/bin/python
TEAMKIT=/srv/example/team-kit
CONFIG=/srv/example/gui/config.json
STATE=/srv/example/gui/state.sqlite
BINDINGS=/srv/example/gui/bindings.json
"$PYTHON" "$TEAMKIT/teamkit_app.py" --config "$CONFIG" --state "$STATE" --bindings "$BINDINGS" view
```

기존 카드의 제목·work ID·현재 문서 revision/digest·담당자·연결 task·대기 이유를 대조합니다. 같은 업무의 새 세션이나 재시도는 새 카드로 복제하지 않습니다. 전송 결과가 불명확하면 기존 요청 ID·수신 기록·실행 주체를 확인한 뒤 다음 행동을 정합니다.

## 2. 업무 등록과 실행 연결

제품 업무는 `work create`로 등록합니다. 비공개 plan에는 목적·변경 내용·검증·영향·사용자가 결정할 내용을 쓰고, contract에는 허용 경로와 완료 조건을 둡니다. 전문용어는 유지하면서 처음 나올 때 쉬운 뜻을 붙입니다. 원문·계약 형식과 승인·버전 변경 절차는 [설계 승인 지침](approved-workflow.md)을 따릅니다.

```sh
"$PYTHON" "$TEAMKIT/teamkit_cli.py" --config "$CONFIG" --state "$STATE" work create WORK-1 \
  --team alpha --root /srv/example/team-alpha/repo \
  --plan-file /srv/example/private/plan.md --contract-file /srv/example/private/contract.json
"$PYTHON" "$TEAMKIT/teamkit_cli.py" --config "$CONFIG" --state "$STATE" work submit WORK-1
"$PYTHON" "$TEAMKIT/teamkit_cli.py" --config "$CONFIG" --state "$STATE" work show WORK-1
```

사용자 승인은 해당 revision/digest의 명시적 결정으로 기록합니다. 구현 task는 `create TASK-1 ... --work WORK-1`로 연결합니다. 새 설계 버전은 `work revise`로 만들며, 이전 승인·실행을 새 버전에 자동 적용하지 않습니다. `create`는 등록이고 실제 배정·수신·실행과 구분합니다. 현재 CLI의 `--help`가 인자 형식의 기준입니다.

완료 조건: 기존 카드와 중복되지 않는 work ID가 있고, 설계·담당 팀·실행 task가 같은 업무에 연결되어 있습니다. 등록만 한 상태를 실행 중이라고 보고하지 않습니다.

## 3. 읽을 수 있는 카드

카드의 짧은 제목은 결과나 작업을 드러내고, 요약은 무엇을 왜 하는지 1~2문장으로 씁니다. 현재 활동에는 실제로 확인한 행동을, 대기 이유에는 누가 무엇을 해야 하는지를 적습니다. “진행 상태 확인 필요”만으로 사용자가 결정해야 할 요청을 만들지 않습니다.

비공개 bindings의 `card_briefs`를 관리하는 운영자는 `work:WORK-1` 설명을 현재 문서의 `document.revision/digest`에 연결합니다. 지원 필드는 `title`, `summary`, `purpose`, `changes`, `checks`, `impact`, `decision_explanation`, `current_activity`입니다. 계획이나 일반 작업 내용을 실시간 실행 관찰처럼 쓰지 않습니다. 문서 버전·내용이 달라지면 새 설명을 확인하며, 원문을 요약문으로 덮어쓰지 않습니다.

현재 설계 revision/digest와 연결된 task만 현재 세션으로 표시됩니다. 신원이 검증된 native `working` 관찰이 실행의 근거이며, 세션 미연결·과거 세션·관찰 실패는 각각 구분합니다. 세션 이름이나 문구를 바꿔 연결을 가장하지 않습니다.

## 4. 사용자에게 결정 요청

실제 결정 대상과 선택에 따른 다음 행동이 있을 때만 질문을 만듭니다. 환경 준비·실행 관찰 실패·전달 결과 불명은 담당자의 운영 확인으로 남깁니다. 선택지는 이 작업에서 가능한 대안으로 작성하고, 각 설명에 누가 무엇을 진행하는지와 승인·의견 전달의 차이를 적습니다.

현재 공식 담당 팀장은 자신의 실제 관리 터미널과 owner·attempt로 `card-question-set`을 호출합니다. task ID나 `HC_TERMINAL_ID`를 만들어 권한을 대신하지 않습니다. 다른 세션은 담당자에게 전달할 질문 초안을 준비할 수 있지만 담당자의 신원으로 저장하지 않습니다.

```json
{
  "prompt": "이번 변경을 어떤 범위로 검토할까요?",
  "options": [
    {"option_id": "focused", "label": "핵심 흐름부터 검토", "description": "담당 팀장이 핵심 사용 흐름의 검토 범위를 먼저 정리합니다."},
    {"option_id": "revise", "label": "설명부터 보완", "description": "담당 팀장이 설계 설명을 보완해 다시 확인을 요청합니다."}
  ]
}
```

이 파일은 의견 전달용 질문이며 설계 승인 명령이 아닙니다. 선택지는 1~4개, `option_id`는 중복 없는 소문자·숫자·`-`·`_` 값입니다. `other`는 예약값이며 기타 입력은 도구가 제공합니다.

```sh
"$PYTHON" "$TEAMKIT/teamkit_app.py" --config "$CONFIG" --state "$STATE" --bindings "$BINDINGS" \
  card-detail --kind work --work WORK-1
"$PYTHON" "$TEAMKIT/teamkit_app.py" --config "$CONFIG" --state "$STATE" --bindings "$BINDINGS" \
  card-question-set --kind work --work WORK-1 \
  --task LEADER-1 --owner current-owner --attempt 1 \
  --question-file /srv/example/private/question.json --expected-question-revision 0
```

`expected-question-revision`은 직전에 읽은 값이며 처음만 0입니다. 충돌하면 현재 질문을 다시 읽고 작성합니다. 이미 제출·승인한 문서가 바뀌었으면 담당자가 버전을 정리한 뒤 다시 제출합니다.

완료 조건: `card-detail`과 `view`에서 같은 현재 질문이 보이고, 선택 설명이 실제 다음 행동과 일치합니다. 다른 질문 버전에 대한 답변을 현재 질문에 적용하지 않습니다.

## 5. 답변 수신과 실제 완료

사용자의 클릭·답변 저장·전달 접수·담당자 수신·업무 완료는 별개입니다. 카드에서 보낸 요청을 받은 담당자만 메시지에 포함된 `card-receipt` 명령의 요청 ID·owner·attempt로 처리 결과를 기록합니다. `responded`는 응답 처리, `deferred`는 나중 처리이며 제품 완료를 뜻하지 않습니다. 보냈다는 이유로 수신을 대신 기록하거나 불명확한 전송을 반복하지 않습니다.

실제 업무는 현재 소스에서 검증 근거를 `work record`로 기록하고 독립 검토를 거쳐 `work finish`합니다. 구체적인 승인·소유권·단계 조건은 [설계 승인 지침](approved-workflow.md), 인계와 결과 불명 처리는 [운영 절차](operations.md)를 따릅니다.

## 6. 보드 상태와 보관

사용자의 보드 상태 선택은 업무 장부의 phase·승인·검증·실제 세션 상태와 분리된 관리 상태입니다. 대기·진행·완료를 직접 선택해도 구현을 시작하거나 설계를 승인하거나 검증 완료를 기록하지 않습니다. 자동 상태로 돌리면 실제 기록과 관찰에 따른 분류를 다시 사용합니다. 보관한 카드는 보관 목록에서 복원합니다.

| 선택 | API action | 조회한 `board.manual_status` | 의미 |
|---|---|---|---|
| 대기 | `waiting` | `waiting` | 보드의 대기 열에 둠 |
| 진행 중 | `progress` | `progress` | 보드의 진행 열에 둠. 실제 실행 관찰은 별도로 표시 |
| 완료 | `done` | `complete` | 보드 정리 완료. 실제 업무 검증 완료와 구분 |
| 자동 | `restore` | `null` | 실제 기록·관찰에 따른 분류로 복귀 |

`archive`는 보관 목록으로 옮깁니다. 보관 목록의 `restore`도 자동 상태로 복귀하며, 보관 전 수동 상태를 재적용하지 않습니다.

저장은 `card-board-action`을 사용하고 직전에 읽은 `board.revision`과 고유한 요청 ID를 전달합니다. SQLite나 보드 journal을 직접 수정하지 않습니다. 같은 요청의 재확인은 같은 요청 ID를 쓰며, 충돌이나 결과 불명 때 현재 `card-detail`을 먼저 조회합니다. 새 설계·질문 내용이 생기면 이전 수동 상태를 이어받아 숨기지 않고 다시 표시합니다.

```sh
"$PYTHON" "$TEAMKIT/teamkit_app.py" --config "$CONFIG" --state "$STATE" --bindings "$BINDINGS" \
  card-board-action --kind work --work WORK-1 --board-action progress \
  --board-revision REVISION_FROM_CARD_DETAIL --request-id board-example-0001
```

완료 조건: 다시 조회한 카드에 선택한 보드 상태가 남고, 실제 업무·승인·전달 기록은 그대로입니다. 보드의 완료 표시만으로 업무 완료를 보고하지 않습니다.

## 공유 범위

팀의 공통 지침 또는 `references`에서 이 문서를 연결합니다. 새 배정은 현재 지침 경로를 받으며, 진행 중인 세션은 다음 카드 관련 작업 전에 다시 읽습니다. 파일이 공유된 사실과 이미 실행 중인 세션이 읽은 사실은 구분합니다. 지침을 배포하려고 기존 대화를 재시작하거나 승인되지 않은 메시지를 보내지 않습니다.

### 영속 업무 큐 카드

`queue status --board`는 실제 티켓만 카드로 표시합니다. 대화에서 포착한 미분류 원문은 아직 실행 티켓이 아니며 운영 정보의 `intake_pending`에 남습니다. 분류 담당자는 정보 질문·일상 대화를 `{"non_task":"구체 이유"}`로 제출합니다. 목표·범위가 불분명한 실제 업무에는 `needs_clarification`을 사용합니다. 물음표만으로 업무 여부를 판단하지 않습니다.

큐 카드의 수동 정리는 `jobs[].board_state.revision`과 고유 요청 ID를 사용합니다. 동작은 위의 보드 상태와 같고, 원 티켓의 실행·승인·검증 기록을 바꾸지 않습니다. 제거는 되돌릴 수 있는 보관이며 진행 중 실행을 중단하지 않습니다.

```sh
"$PYTHON" "$TEAMKIT/teamkit_cli.py" --config "$CONFIG" --state "$STATE" queue organize TICKET-1 \
  --board-action archive --client-request-id queue-example-0001 --expected-revision CURRENT_REVISION
```

기존 비업무 접수는 원문과 수신·시도 기록을 대조한 뒤 `queue dismiss INSTRUCTION_ID --reason REASON --expected-digest ORIGINAL_TEXT_SOURCE_DIGEST`로 제외합니다. digest는 원문의 `text`와 `source` 객체를 기존 ticket digest 규칙으로 계산합니다. 이미 업무 티켓이 연결되면 제외를 거절합니다. 제외는 원문·시도·분류 제출 기록을 삭제하지 않고 별도 disposition 이력을 추가합니다. 늦은 분류는 제외된 원문에서 새 업무를 만들 수 없습니다. 실행 슬롯은 제외만으로 해제하지 않으며 기존 실행자의 실제 대기 상태를 관측한 뒤 반환합니다.
