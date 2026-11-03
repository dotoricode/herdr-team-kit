# 운영 절차

## 팀장과 작업

제품별 대표 진입점과 작업별 책임 팀장은 하나입니다. 실제 세션은 작업·계정·컨텍스트 상태에 따라 교체할 수 있습니다. 전체 활성 모델 작업 상한은 3개이며 제품별 구현 워커는 1개입니다. 같은 root의 구현도 직렬화합니다. 대기 중인 팀장은 `ready`로 두고 모델 상한에서 제외합니다. Herdr에 직접 입력한 외부 작업은 이 장부의 동시성 제어에 포함되지 않습니다.

작업은 `queued → claimed → launching → submitted → running`으로 진행합니다. 전송 응답은 수신 확인이 아닙니다. 에이전트가 `receipt`로 계정을 대조해야 running이 됩니다. 모델과 effort는 요청값을 기록하며 실제 적용은 statusline·런타임으로 별도 확인합니다. Herdr의 idle/done도 작업 검증 완료는 아닙니다.

계획·지침은 `references` 경로를 통해 필요한 부분만 읽습니다. 작업 범위·기준 SHA·검증·다음 행동을 기록하고 제품 간 계약은 담당 팀장과 조정합니다. ai-memory는 제품 범위의 관련 과거 지식만 조회합니다. 전체 대화 복제와 불필요한 상태 확인용 모델 호출은 피합니다. 이 도구는 ai-memory 권한·수동 저장 정책을 바꾸지 않습니다.

## 계정과 복구

팀계정의 5시간·주간 사용량이 모두 90% 미만이면 팀계정을 우선합니다. 어느 한 윈도가 90% 이상이면 개인계정을 검토합니다. 누락·만료·미래 시각·잘못된 값은 사용 가능으로 취급하지 않습니다. 양쪽 모두 사용할 수 없으면 새 모델 작업을 대기합니다. 결제·API 계정으로 자동 전환하지 않습니다.

선택적으로 `account_selection`에 `timezone`, `weekdays`, `weekends`를 지정할 수 있습니다. 예를 들어 `{"timezone":"Asia/Seoul","weekdays":["team","personal"],"weekends":["personal","team"]}`는 해당 시간대의 토·일요일에 개인계정을 우선합니다. 두 순서에는 `team`과 `personal`이 각각 한 번씩 있어야 합니다. 이 설정은 route, 새 배정, 신원 확인 뒤 재검사와 총괄 복구에 동일하게 적용되며, 실행 중인 작업을 다른 계정으로 옮기거나 한도·승인 검사를 생략하지 않습니다. 미설정 시 기존 팀계정 우선 순서를 유지합니다. 공급자·모델은 기존 역할 및 명시적 사용자 결정으로 정하며, 계정 순서가 공급자 전환을 승인하지는 않습니다.

일반 팀장 작업과 일반·소규모 구현 작업에서 공급자 간 대체 실행을 허용하려면 다음 설정을 추가합니다.

```json
{"execution_preference":{"provider_order":["claude","codex"],"weekend_account_first":true}}
```

이 설정과 위 계정 일정의 조합은 주말에 **개인 Claude → 개인 Codex → 회사 Claude → 회사 Codex** 순서로 새 배정을 고릅니다. 개인 Claude를 사용할 수 없어도 개인 Codex가 가능하면 회사 Claude보다 먼저 선택합니다. 평일에는 **회사 Claude → 개인 Claude → 회사 Codex → 개인 Codex** 순서입니다. `account_selection`을 함께 지정하면 해당 시간대와 평일·주말 계정 순서를 따릅니다. 계정 일정 없이 이 설정만 켜면 `Asia/Seoul` 기준 평일은 회사, 주말은 개인을 먼저 검토합니다. `weekend_account_first=false`이면 주말에도 공급자 순서 안에서 계정 일정을 적용합니다.

공급자를 바꾸는 경우 선택한 공급자·계정 프로필의 모델을 쓰고 작업의 effort는 유지합니다. 같은 공급자는 역할·작업 유형에 지정한 모델을 유지합니다. 소규모 구현의 기본 모델도 선호 정책이므로, 개인 Claude를 사용할 수 없으면 개인 Codex를 먼저 선택하고 low effort를 유지합니다. 개인 Claude가 가능하면 소규모 작업에 지정한 모델을 그대로 사용합니다. 명시적 `user_decision`, 리뷰어, 설계, 위험 분류가 있는 작업은 공급자·모델을 바꾸지 않고 기존 계정 일정만 적용합니다. 이 설정이 없으면 기존 공급자 고정 배정을 유지합니다. `plan`은 이 설정이 있을 때 현재 한도에 따른 실제 후보를 보여주며, 신원 조회나 실행은 하지 않습니다.

제품 팀장의 명시적 기본 우선순위는 `leader_execution_preference`로 별도 지정할 수 있습니다.

```json
{"leader_execution_preference":{"provider":"claude","account":"team","model":"claude-opus-5-5","effort":"medium"}}
```

이 설정은 실제 팀장 역할의 새 배정에서 해당 모델·계정을 평일·주말 모두 먼저 검토합니다. 일반·설계·위험 분류에 동일하게 적용하며 명시적 사용자 모델 결정과 요청의 계정·공급자 고정은 보존합니다. 이후 후보는 기존 `execution_preference`와 계정 일정에 따르며, 선호 계정의 인증·사용량·세션·경로 검사를 우회하지 않습니다. 미설정 시 기존 정책을 유지합니다. 조사 담당처럼 팀장 모델 정책에 매핑된 다른 역할에는 이 예외를 적용하지 않습니다. `work_id`에 연결된 기존 설계 전용 실행자는 별도 Codex sandbox 어댑터와 기존 모델 정책을 유지합니다. 팀장은 이 실행자에게 설계를 위임하며 기존 쓰기 격리를 바꾸지 않습니다. 실행 중인 세션을 변경하거나 기존 입력을 재전송하지 않습니다. `plan`과 신원 조회 후 재검사도 이 설정만 단독 지정한 경우 현재 한도와 설정을 대조합니다.

팀장은 제품 판단·분해·승인 범위·결과 검토를 책임집니다. 승인된 구현·테스트·검증은 실제 사용 가능한 위임 도구와 자원 장부를 통해 경계가 명확한 워커에게 맡깁니다. 독립 작업만 병렬화하고 파일·worktree별 작성자를 분리합니다. 전달은 목표·완료 조건·승인 경계·관련 경로·소스 버전으로 제한하고, 회신은 결과 경로·SHA·검사 결과로 받습니다. 위임 불가 시 이유와 다음 행동을 대기 상태로 기록합니다. 준비 전용 세션에서는 작업 배정 전 워커를 생성하지 않으며, 독립 리뷰는 구현자와 다른 담당자에게 맡깁니다.

배정은 현재 프로필에 연결된 최신 한도 근거만 사용합니다. 신원 확인 뒤 공급자·모델·계정·effort와 프로필 연결을 다시 선택해 대조하며, 달라지면 claim 전에 중단합니다. 기존 승인·소유권·동시성 검사는 그대로 적용합니다. 이 선택 순서는 실행 중인 작업을 옮기거나 API 계정으로 전환하지 않습니다.

Codex 공식 응답의 `ordinaryUsageAllowed=true`이고 유효한 알려진 윈도가 하나 이상일 때만 제공되지 않은 다른 윈도를 `not_applicable`로 기록합니다. 예를 들어 주간 윈도만 제공된 구독을 5시간 한도 누락으로 차단하지 않습니다. 제공된 각 윈도는 계속 90% 미만·유효한 초기화 시각·TTL을 충족해야 합니다. 명시적 일반 사용 차단과 서버가 `rateLimitReachedType`으로 분류한 한도·크레딧 차단은 낮은 사용률보다 우선하여 배정을 막습니다. `ordinaryUsageAllowed=true`이고 서버의 분류된 차단이 없으면 추가 크레딧의 `spendControlReached=true`만으로 기본 포함 사용을 막지 않습니다. 명시적 기본 사용 허용 없이 지출 제어에 도달한 경우에는 계속 배정을 막습니다. 허용 정보가 없거나 잘못된 기존 자료에서 적용 제외를 추정하지 않습니다. Claude의 검증 기준은 바꾸지 않습니다.

진행 작업은 안전한 경계까지 유지합니다. 팀계정 복구는 새 작업부터 적용합니다. 같은 계정·같은 작업의 resume은 native CLI로 수행하고 기존 소유권을 유지합니다. 현재 dispatch는 교차 계정 인계 후 새 세션을 시작합니다. Codex 새 실행과 resume에는 `--no-daemon`을 넣어 관리 pane의 실행 환경을 공유 background server에 맡기지 않습니다. 실제 Herdr 환경 전달은 별도 통합 검증 대상입니다. 인증 파일이나 세션 저장소를 공유하지 않습니다. 기존 로그인 홈을 재사용할 때도 실제 계정 일치를 먼저 확인합니다. Claude의 정확한 기본 홈은 기본 keychain namespace로 실행하고, 별도 홈은 `CLAUDE_CONFIG_DIR`로 분리합니다. 기본 경로를 명시하는 것과 환경변수를 비우는 것이 같은 인증 namespace라고 가정하지 않습니다. 기본 프로필의 receipt·capture는 환경변수가 unset 또는 빈 값일 때만 허용하며, 명시적 기본 경로는 인증 조회 전에 거절합니다.

새 Codex 배정의 receipt는 host 중계를 사용합니다. worker는 지정된 profile 환경을 확인하고 owner·attempt·nonce·profile binding·만료 시각에 묶인 요청을 남깁니다. 배정 host가 요청 이후 실제 provider identity를 조회하고, 요청과 현재 설정·소유권·기한을 다시 대조한 뒤 수신을 확정합니다. worker sandbox에 인증 홈 쓰기 권한을 추가하거나 인증 파일을 복사하지 않습니다. 기존 preflight만 재사용하지 않으며 `identity_source=host-fresh-provider`로 출처를 구분합니다. 이는 로컬 장부의 attestation이며 native 세션 인증을 암호학적으로 증명하지 않습니다.

host 확인은 배정의 prompt 대기 동안만 동작합니다. 요청 기한 만료·배정 종료·실패·소유권 변경은 자동 재배정하지 않습니다. dispatcher가 끝난 뒤 늦게 생긴 요청은 확인되지 않으며, 기존 owner와 실행 상태를 조사해 명시적으로 인계한 뒤 새 배정을 결정합니다. 기존 parked leader 및 Claude receipt는 이 중계로 자동 전환하지 않습니다.

시간 초과·전송 불명은 자동 재시도하지 않습니다. 같은 소유자의 늦은 receipt는 실제 프로필·계정 확인 후 running으로 복구할 수 있습니다. `show`와 `events`, Herdr 출력으로 실제 프로세스와 소유권을 확인합니다. 기존 작성 주체가 멈춘 것을 확인한 뒤 다음 형식의 로컬 파일로 인계합니다.

```json
{"verification": ["command, result, and evidence location"], "next": "next bounded action"}
```

```bash
python3 teamkit_cli.py handoff TASK-1 --owner OWNER --writer-stopped --evidence-file /srv/example/private/checkpoint.json
```

현재 HEAD·dirty 상태·worktree 지문을 추가해 보존합니다. 계정 변경 후에도 같은 작업 ID를 사용하며, 지문·HEAD가 달라졌으면 먼저 대조합니다. `--writer-stopped`는 운영자가 확인한 사실의 기록이지 실행 중 프로세스를 종료하는 명령이 아닙니다.

검증을 끝낸 워커는 같은 파일 형식의 `verification` 근거로 `complete`를 호출합니다. 수신 확인된 running 상태에서만 완료할 수 있습니다. 결과는 `awaiting_review`이며, 독립 검토가 남습니다. 리뷰어는 추가로 `reviewed_head`에 기준 SHA를 기록하고, Git의 index·HEAD·현재 branch lock을 보유한 상태에서 현재 HEAD와 clean 여부를 확인해 완료합니다. files ref 저장 형식만 지원하며 reftable 등은 lock을 만들기 전에 거절합니다. 다른 Git 작업의 lock은 제거하지 않습니다. 프로세스 강제 종료로 자체 lock이 남으면 소유 프로세스 종료를 확인한 뒤 별도로 복구합니다. 기밀·원본 diff는 근거 파일이나 공개 PR에 복사하지 않습니다.

## 컨텍스트와 측정

컨텍스트 판단은 80%에서 준비, 90% 이상에서 안전한 경계의 인계입니다. `context --used 90 --safe-boundary`는 판단만 반환합니다. 실제 자동화는 Core 앱 설정의 `context_handoff`를 `{"enabled":true,"threshold":90,"interval":10}`으로 켜고, 절대 경로 `context_bindings`를 지정한 경우에만 작동합니다. TUI와 관리되지 않는 사용자 세션은 자동 제어하지 않습니다.

`auto-context serve`는 정확한 세션의 컨텍스트 사용률을 관측합니다. Claude는 원 응답에 연결된 statusline, Codex는 등록된 세션의 최신 token_count를 읽습니다. 누적 토큰과 구독 한도는 컨텍스트 수치로 사용하지 않습니다. 별도로 현재 계정의 검증된 5시간·주간 사용률이 90% 이상이거나 Codex의 일반 사용이 명시 차단된 경우에도 인계를 예약합니다. 컨텍스트와 구독 한도는 원래 지표·출처·시각을 각각 보존합니다. 조회 실패·미확인은 한도 소진으로 해석하지 않습니다. 기존 Codex의 일회 등록은 `auto-context enroll`, 신규 Codex receipt와 총괄 시작은 실제 세션 환경에서 등록합니다. 관측은 원 시각을 유지하며 오래되거나 미확인인 값으로 인계를 시작하지 않습니다.

90% 관측은 한 번만 예약합니다. 작업 중·입력 초안·확인 창은 기다립니다. 기존 담당이 남은 승인 업무·검증·승인 대기를 저장하고 background writer 중단을 확인한 후, 정상 종료와 작업 지문을 검증합니다. 그 다음 공식 handoff/dispatch/receipt를 사용합니다. 새 계정의 최신 한도·신원과 기존 작업 승인·revision이 유효해야 재개합니다. 계정·모델은 일반 배정과 같은 실행 후보 순서를 재사용하며, 명시 모델과 리뷰 역할의 고정 조건을 유지합니다. ready 팀장은 기록을 복구하고 대기하며, 총괄 교체는 제품 업무를 자동 재개하지 않습니다. 원 담당 대화 파일은 삭제하지 않습니다. 한도가 완전히 소진돼 인계 기록을 작성할 수 없으면 쓰기 중단을 추정하지 않고 확인 필요로 대기합니다. 저장 후에도 종료 직전 현재 세션을 다시 확인하며, 변경·미확인은 기존 실행을 보존합니다.

전송·종료·배정의 결과가 불명확하면 중복 실행하지 않고 확인 필요로 남깁니다. 사용 가능한 계정이 없으면 기다리며 공식 한도 조회는 최대 5분에 한 번입니다. 자동 인계는 절대 무중단을 보장하지 않습니다. 사람이 답해야 하는 질문·만료된 승인·인증 실패는 우회하지 않습니다. 보드에는 인계 또는 확인 대기를 표시합니다. `auto-context status`로 원인과 감시 대상을 확인할 수 있습니다.

새 Core 배정은 필요할 때 감시 프로세스를 확인·시작합니다. 앱을 닫아도 Core의 감시는 유지됩니다. Core나 호스트가 종료되면 다음 관리 배정에서 다시 확인합니다. `verify-request`는 `CONTEXT-SMOKE-` 이름과 비공개 계획 아래의 독립 작업에만 허용하는 수명주기 검사이며, 실제 90% 도달을 주장하지 않습니다.

유형별 완료 5건 또는 2주 시점에 초기 평가합니다. 안정화 중 주 1회, 이후 월 1회와 주요 업데이트 후 평가합니다. 완료 시간·호출·재시도·입출력 토큰·cache read/write·한도 변화·재작업을 따로 측정합니다. 현재 도구는 비용·절감률을 산출하지 않습니다. MCP는 긴 로그와 반복 왕복을 줄이는지 CLI와 비교합니다.

## 공유 매뉴얼

제품별 고객 매뉴얼과 내부 운영 매뉴얼은 로컬 설정에서 정본을 연결합니다. 공유 매뉴얼은 최신 `origin/deploy-staging` SHA를 고정하고 별도 작업 브랜치·worktree에서 수정합니다. 공통 계약과 같은 파일 변경은 총괄이 조정합니다.

로컬 저장소에 다음 스크립트가 있는 경우 최초 `./local-dev.sh full`, 수정 후 다른 터미널에서 `./local-dev.sh antora`를 실행합니다. 이 명령은 범용 kit에서 자동 실행하지 않습니다. 기존 서버의 소유권·포트를 확인하고 생성 경로를 갱신합니다.

완료 보고에는 수정 파일·절·내용, 브랜치·검증 SHA·dirty 상태·빌드 시점, 실제 빌드 페이지 URL·화면과 내비게이션·링크 확인을 포함합니다. 빌드·테스트·브라우저 확인은 각각 구분합니다. 실패·미실행은 완료로 보고하지 않습니다.

## PR와 외부 반영

GitHub 변경은 PR 생성 → Codex 독립 리뷰 → 지적 검증·수정 → 최신 SHA 재리뷰 → 필수 검사 → merge 순서입니다. 자동 수정·재리뷰는 3회 후 총괄에 보고합니다. 미해결 지적·실패·시간 초과·오래된 SHA의 리뷰는 머지 근거가 아닙니다. 현재 CLI는 GitHub 머지나 리뷰 판정을 자동 실행하지 않습니다.

리뷰는 확인한 커밋을 적어 PR의 코드 줄별 스레드에 남깁니다. 문제가 없을 때도 핵심 변경 줄에 확인 기록을 남깁니다. 수정·답변·관련 커밋·다시 확인한 결과는 같은 스레드에 이어 적고, 끝난 확인은 마무리 표시를 합니다. 로컬 문서는 이 기록을 대신하지 않습니다.

리뷰를 먼저 요청하고 기다리는 동안 허용된 범위의 별도 검사·다른 작업·문서·설치 준비를 진행합니다. 확인 중인 코드는 고정하고 새 작업은 별도 브랜치로 나눕니다. 코드가 바뀌면 새 커밋을 다시 확인받고, 최신 리뷰·필수 검사·남은 문제 확인을 끝낸 뒤 머지합니다.

내부 Git의 push·PR 게시·merge는 각각 사용자 승인을 받습니다. 내부 PR 내 리뷰 절차는 적용하지 않되 구현과 독립 검증은 구분합니다. 읽기용 브라우저 접근과 게시 승인은 별개입니다. GitHub 승인 범위를 내부 Git으로 확대하지 않습니다.

Receipt waiters record SIGINT/SIGTERM cancellation before exiting. SIGKILL or host/process failure can prevent cleanup; inspect and hand off the stopped owner before retrying. A ledger receipt is not process-liveness proof.

## 팀장 작업 큐

`pool assign --team TEAM --work WORK-ID --message-file FILE --provider codex --account team`은 요청을 디스크 큐에 넣습니다. `--completion-file FILE`로 최종 보고 주체·검증·산출물 등 완료 조건을 함께 고정할 수 있습니다. 같은 팀의 같은 work ID와 동일한 본문·계정 제약·완료 조건은 기존 요청으로 합칩니다. 다른 내용으로 같은 ID를 덮어쓰지 않습니다. 서로 다른 작업은 접수 순서대로 보존합니다. work ID 생략 시 본문 해시를 키로 사용하며, 같은 본문을 새로운 업무로 요청하려면 새 work ID를 지정합니다.

`received`는 전달과 수신 확인이며 업무 완료가 아닙니다. 기존 `done` 기록도 수신만 확인한 것으로 보존하고 다음 작업을 막습니다. 앞선 업무가 `completed` 또는 `deferred`로 확인되기 전에는 native가 idle이어도 다음 요청을 보내지 않습니다. `uncertain` 요청은 자동 재전송하거나 resolve하지 않으며 이후 작업도 기다립니다. 기존 기록은 삭제하거나 업무 완료로 승격하지 않습니다.

`pool resolve --request REQUEST-ID --outcome completed|deferred --evidence-file JSON --expected-owner OWNER --expected-attempt N`은 수신된 업무만 정리합니다. 현재 장부와 수신자의 owner·attempt·terminal이 일치하고 native가 idle이며 입력 초안이 없어야 합니다. JSON에는 `work_id`, `owner`, `attempt`, `writer_stopped: true`, 비어 있지 않은 `verification` 문자열 배열을 넣습니다. 고정 완료 조건이 있으면 동일한 `completion_requirements` 원문과 그 조건을 실제 검증한 근거가 필요합니다. `deferred`에는 인계할 `next`도 필요합니다. 팀장 초안만 있는 것을 다른 담당자의 최종 보고 완료로 선언해서는 안 됩니다. 이 명령은 검증 결과의 명시 기록이며 서버·모델·게시 승인 범위를 확대하지 않습니다.

긴 제품 작업이 native 전달 대기 시간을 넘겨 `uncertain`이 되면 자동 재전송하지 않습니다. 총괄이 실제 ACK·화면·소유권을 대조해 수동으로 정합화해야 하며, 이 최소 큐는 그 결과를 추정하지 않습니다. 업무 수신 뒤 auto-context 등으로 owner가 바뀐 경우에도 기존 receiver와 현재 owner가 달라 resolve를 거절하고 수동 확인을 기다립니다. 소유권 인계 chain의 자동 추적과 불명 수신 정합화 인터페이스는 이번 범위에 포함하지 않습니다.

`pool status`는 요청별 work ID·계정 제약·완료 조건과 안전한 오류 단계·코드를 표시합니다. 오류 원문은 저장하지 않으므로 이 코드만으로 과거 native 실패의 정확한 원인을 단정하지 않습니다. supervisor와 현재 Python 소스의 fingerprint가 다르면 새 assign은 거절합니다. active jobs와 전달 불명을 먼저 확인하고 supervisor만 안전하게 재기동해야 하며 기존 팀장 세션은 재시작하지 않습니다. 새 supervisor는 실행 중 소스 변경을 감지하면 다음 tick을 중단하고 진행 중 작업이 끝나기를 기다립니다.

## 담당자 우선 전송

`queue candidates TICKET`은 배정 불가 직원도 모두 보여 주며 각 후보에 원문 `reason`, 한국어 `reason_ko`, `override_allowed`를 담습니다. 배정 불가 후보에도 사용자가 직접 지정하면 `queue send TICKET --candidate-id ID --expected-ticket DIGEST --client-request-id KEY --override-eligibility --reason "사유"`로 보낼 수 있습니다. `--reason`은 1~300자 필수이고 `--override-eligibility` 없이 쓸 수 없습니다.

우회하는 것은 역할·책임·모델 정책, 계정 용량(전체·계정), 사용량, 업무에 적힌 계정 목록입니다. 우회 사실·사용자 사유·미리보기 때 원래 사유·실행 시점에 실제로 무시한 사유는 큐 action 기록(`request.override`, `result.override`)과 attempt(`override`)에 남습니다.

우회하지 않는 것: 업무 승인·고정 승인 기록, `pending`이 아닌 업무의 재전송 금지, 이전 선택이 queued·running·uncertain일 때의 재선택 금지, UI 업무, 새로 만들 직원(계획 항목)의 용량 우회, 다른 attempt와의 터미널 세션·쓰기 자원 충돌, 전송 시점의 소유자·프로세스 확인과 입력 초안·확인 창 검사. 마지막 둘은 전송 직전에 다시 읽어 입력 중인 내용이 있으면 전송하지 않고 blocked로 보고합니다.

우회 전송은 supervisor가 새 소스를 읽어야 실제 반영됩니다. 위 `팀장 작업 큐`의 fingerprint 규칙에 따라 active jobs와 전달 불명을 확인한 뒤 supervisor만 안전하게 재기동합니다.

## 운영 현황과 계정 사용률

`monitor`는 호스트에서 native 실행 상태를 3초마다 관측하고 Codex 회사/개인 사용률을 30초마다 조회합니다. provider 조회는 별도 스레드에서 실행하여 지연된 한도 조회가 실행 상태 관측을 막지 않습니다. Claude는 기존 실제 응답에 연결된 statusline 수집을 유지합니다. 한도 확인을 위한 모델 턴·API fallback은 만들지 않습니다.

```bash
python3 teamkit_cli.py monitor
python3 teamkit_cli.py dashboard --publish
```

두 프로그램은 별도 터미널에서 실행합니다. dashboard는 읽기 전용이며 monitor는 장부의 quota와 별도 관측 파일만 갱신합니다. 각 서비스는 단일 실행 잠금을 사용합니다. 관측이 없거나 오래됐으면 실행 상태를 미확인으로 표시합니다. 한도 누락·계정 binding 불일치·만료는 0%가 아닙니다. 이전 자료는 별표와 수집 경과를 표시하며 조회 실패는 새 배정 가능 근거로 사용하지 않습니다.

`설계 검토 필요`는 모델의 권한 팝업과 다릅니다. 현황에 표시한 비공개 문서에서 대안·범위·검증을 확인하고 총괄 대화에서 선택과 승인을 남깁니다. 제품 업무의 미완료 건수와 native 작업 중/대기/입력 요청은 별도로 표시합니다.

## 종료된 임시 작업 공간

새 workspace 표시는 `업무ID · 역할 · 팀`이며 내부 고유 agent 이름과 분리합니다. 상시 팀장과 작업별 설계 실행도 구분합니다. 작업 호스트는 완료 보고 전에 결과·인계 자료를 보존하고 native idle/done과 백그라운드 작업 종료를 직접 확인한 뒤 임시 공간을 정리합니다. idle 자체를 작업 검증 완료로 간주하지 않습니다.

```bash
python3 teamkit_cli.py retire TASK-1 --owner OWNER --writer-stopped --evidence-file /srv/example/private/retirement.json
```

근거에는 비어 있지 않은 `verification` 목록을 넣습니다. 이 명령은 호스트의 정지 확인을 요구하며 자동 janitor가 아닙니다. 새 실행기가 만든 임시 workspace만 허용하고, 기존 owner·workspace label/layout·native foreground PID와 상태를 대조합니다. 상시 팀장·현재 workspace는 보호하며 결과·설계·Git/worktree 파일은 삭제하지 않습니다. 닫기 결과가 불명이면 intent를 보존하여 재시도 대신 inspect하게 합니다. 이미 닫은 실행에 tell을 보내지 않으며 후속 실행은 근거를 보존해 handoff한 뒤 배정합니다.

## 읽기 전용 장부 조회

`show`, `list`, `events`, `work show`는 기존 SQLite 장부를 `mode=ro`로 읽습니다. 쓰기용 Store 초기화·chmod·스키마 생성·승인 트랜잭션을 실행하지 않으며, 조회 때문에 디렉토리나 장부를 만들지 않습니다. 미존재 장부는 명시적인 오류이고, 이전 장부에 work_items가 없으면 unknown product work로 반환합니다. 손상된 DB/레코드와 읽기 잠금 오류도 JSON 오류로 보고합니다. 배타적 잠금의 SQLite 대기 설정은 1초이며 이후 오류로 반환하고, 일반 쓰기 예약 잠금 아래서는 커밋된 내용을 읽습니다. 쓰기·배정·수신·승인 경로는 기존 정책을 유지합니다.

읽기 전용 조회는 기본 rollback 장부를 지원합니다. WAL은 `mode=ro`에서도 보조 파일을 생성·갱신할 수 있어 SQLite 연결 전에 명시적으로 거절합니다. journal mode를 자동 변경하거나 WAL 내용을 무시하는 immutable 조회를 하지 않습니다. 외부 writer가 조회 도중 journal mode를 변경하는 경우는 지원 범위 밖입니다.


## 마우스·트랙패드 스크롤

대화형 화면은 휠이나 세로 스와이프로 위아래를 움직이며, PgUp/PgDn도 사용할 수 있습니다. X10·SGR1006 마우스 입력을 직접 읽어 아래 스크롤 버튼이 없는 macOS 터미널 라이브러리에서도 동작하게 합니다. 클릭은 한 번만 펼치고 버튼을 놓는 입력·가로 스크롤·단순 포인터 움직임은 무시합니다. 화면을 닫거나 오류로 끝나면 켰던 마우스 모드를 끕니다.


## GUI Claude 공식 조회와 배정

GUI core 배정은 실제 응답에 연결된 statusline 자료와 검증된 공식 `/usage` 조회 중 최신 관측을 사용합니다. 같은 시각이면 더 높은 사용률을 적용합니다. 조회 기록은 현재 계정 binding, 원 관측 시각, 두 구간, 토큰 0, 조회 성공과 임시 세션 정리가 모두 맞아야 합니다. 수동 표시값·지난 기록·실패·결과 불명·계정 불일치는 배정하지 않습니다.

공식 조회 출처는 `claude-native-usage`로 구분합니다. 조회가 제공하지 않은 초기화 시각을 만들지 않고 기존 TTL 안에서만 인정합니다. statusline과 Codex의 초기화 시각 검사는 유지합니다. 회사→개인→대기 및 각 구간 90% 미만 기준을 바꾸지 않으며 한도 확인용 모델 턴이나 private endpoint를 호출하지 않습니다. 이 연결은 GUI core 전용입니다.

## 앱 팀장 준비 세션

`session_pool`은 App Core의 일반 팀장 전용 선택 설정이다. `enabled`, 절대 경로 `bindings`, 모든 팀의 고정 팀장 TASK ID를 담은 `tasks`를 지정한다. 설계·리뷰·구현 워커와 명시적으로 모델이 고정된 TASK는 기존 경로를 사용한다.

`pool prepare`는 팀별 Codex/Claude × 회사/개인 네 native 세션을 준비한다. 생성 전에 실제 프로필의 Atlassian MCP 등록을 확인한다. 준비는 TASK claim이나 업무 수신이 아니며 모델 프롬프트를 보내지 않는다. 각 세션은 생성 당시의 예정 owner·프로필·모델·effort·cwd·설정에 고정된다. 공식 배정과 신원·receipt 확인을 마쳐야 업무 담당이 된다. 생성 결과가 불명이면 다시 만들지 않는다.

`pool present`는 기존 팀장 스페이스에 준비 세션을 표시하고 계정·공급자 이름으로 탭을 붙인다. `pool status`는 준비·예약·담당 상태와 요청의 대기 이유를 보여준다. `pool refresh`는 공식 관측을 갱신하며 모델 턴을 시작하지 않는다. 계정 한도는 같은 프로필의 세션들이 공유하므로 팀별로 복제된 사용 가능량으로 계산하지 않는다.

총괄은 `pool plan --team TEAM`으로 현재 정책의 담당 후보를 확인한다. 최초 연결은 `pool bootstrap --team TEAM`으로 receipt와 대기만 확인한다. 이후 승인된 요청 파일은 `pool assign --team TEAM --message-file FILE`로 전달한다. 같은 팀에 미처리 요청이 있으면 다른 요청을 조용히 덮어쓰지 않는다. 선택된 계정의 준비 세션이 없다는 이유로 후순위 계정을 고르지 않는다.

요청에서 공급자·계정을 지정했다면 `pool assign --team TEAM --message-file FILE --provider codex --account team`처럼 두 값을 함께 전달한다. `pool plan --team TEAM --provider codex --account team`은 그 제약으로 조회만 한다. 요청이 안전한 실행 경계에 도달하면 제약을 고정 팀장 task에 기록하며, 이후 공식 plan·dispatch·tell과 컨텍스트 인계가 같은 값을 사용한다. 지정 계정의 유효한 한도가 없으면 대기하고 다른 계정이나 공급자로 넘어가지 않는다. 실행 중인 다른 담당의 제약을 덮어쓰지 않으며, 수신 가능 상태가 될 때까지 기다린다.

대기 중인 현재 담당이 새 선택과 다르면, 요청을 보존하고 정책에 따른 인계를 예약한다. 실행 중인 업무·입력 초안·확인 창은 기다린다. 90% 인계도 다음 공급자·계정·모델·세션 세대를 기록하지만, 예약 자체가 새 claim은 아니다. 실제 배정 직전에 사용량·요일·프로필과 native를 다시 확인하고 후보가 바뀌면 변경 이유를 남긴다. 미완료·승인 대기는 공식 체크포인트와 dispatch prompt로 전달한다.

같은 세션의 다른 owner 재임대는 하지 않는다. 나간 세션의 정상 종료와 공식 owner 해제를 확인한 뒤에만 새 세대로 보충한다. Context만 찬 경우에는 같은 계정의 새 세대가 선택될 수 있다. 구독 한도가 찬 경우에는 기존 우선순위의 사용 가능한 다른 계정을 선택한다. ready·awaiting_review는 빈 슬롯이 아니다. 기존 사용자 대화·초안 있는 세션을 준비 슬롯으로 자동 편입하지 않는다.

기존 한도 소진 팀장을 보존하면서 새 연결용 고정 TASK를 구성할 수 있다. 이때 진행 중인 팀 작업이 없어야 하며 새 팀장의 초기 범위는 수신·기록 확인·대기다. 과거 담당의 writer 중단을 추정하거나 그 업무를 완료·인계 처리하지 않는다. 제품 작업과 승인 기록은 원래 상태로 보존한다.

## 업무 연결과 팀별 확인 담당

`pool assign`의 `--work`는 큐의 안정 업무 ID입니다. 원 제품 장부와 연결하려면 `--workflow WORK-ID`를 함께 지정합니다. 그때의 문서 revision/digest/root를 고정하고, 소비 직전 현재 승인과 허용 경로를 다시 확인합니다. 연결 없는 기존 요청의 의미는 유지합니다. 변경된 설계를 새 요청의 승인으로 자동 승계하지 않습니다.

`--priority 0..9`는 대기 요청 사이의 순서를 조정하며 기본 0은 접수 순서입니다. 이미 수신·실행·불명인 같은 팀의 업무를 선점하지 않습니다. `--depends-on REQUEST-ID`는 여러 번 지정할 수 있고 실제 `completed`만 의존성 충족으로 봅니다. 보류·취소·수신은 완료가 아닙니다. 각 요청은 연결된 제품 root 또는 팀 root를 자원으로 보호합니다. 추가 `--resource /srv/example/shared`는 설정된 팀·공유 root 안에서만 허용하며, 다른 팀의 진행 업무와 부모/자식 경로가 겹치면 기다립니다.

연결된 업무의 큐 완료는 원 workflow의 공식 complete·승인 버전·milestone·별도 최신 reviewer 근거를 확인한 뒤 기록합니다. 일반 프로그램은 이미 수신 확인된 요청만 정합화하며 unknown을 자동 복구하거나 새 전송하지 않습니다. 큐에 별도 `--completion-file` 조건이 있으면 업무 장부 완료만으로 그 조건을 충족했다고 추정하지 않습니다. 자동 마감은 보류하고 추가 조건의 실제 증거를 확인한 명시적 `pool resolve`를 기다립니다. 조건 원문을 자동 복사해 검증 근거로 만들지 않습니다. receiver와 현재 담당이 바뀌거나 입력이 남으면 기존 요청과 담당을 보존합니다. 긴 native 전달 대기 동안 전역 model-start 잠금은 해제하지만, 팀별 소비 잠금과 sending intent·공식 dispatch/tell 내부의 admission 검사는 유지합니다.

팀별 개인 Codex 확인 역할은 선택 설정 `team_dispatch`로 켭니다. 제품 팀장 task와 별도 ID를 사용하며 같은 사용량을 팀별로 복제하지 않습니다.

```json
{
  "team_dispatch": {
    "enabled": true,
    "tasks": {"alpha": "DISPATCH-ALPHA"},
    "stall_seconds": 900
  }
}
```

`tasks`에는 설정한 모든 팀이 필요하며, 기존 core backend·session_pool·context_handoff가 켜져 있어야 합니다. `team-dispatch prepare`는 읽기 전용 판단용 task만 등록합니다. `team-dispatch bootstrap --team alpha`는 명시 실행으로 기존 공식 배정·실제 신원/receipt 경로를 사용해 개인 Codex를 준비합니다. standby를 자동 생성하거나 다른 계정으로 대체하지 않습니다. 공통 실행기는 Codex `--approve-for-me --no-daemon`, Claude `--permission-mode auto`를 사용합니다. 확인 담당은 실제 native 인자·모델·effort와 빈 입력을 대조한 뒤 이벤트를 받습니다. 기존 세션의 mode나 owner를 임의 변경하지 않습니다.

기존 supervisor가 큐의 의미상 변화와 장기 정체를 디스크 이벤트로 기록하고, 판단이 필요할 때만 확인 담당에게 전달합니다. 관측 시각만 바뀌면 새 이벤트나 모델 턴을 만들지 않습니다. 실제 의미가 변한 시각을 별도로 보존하므로 주기적인 pending 갱신이 정체를 숨기지 않습니다. 긴 이벤트 전달은 기존 executor에서 처리하며 별도 AI polling·상주 orchestrator를 만들지 않습니다. 개인 한도·인증·native·입력 확인이 안 되면 이벤트를 보존하고 대기합니다.

확인 담당은 전달 메시지에 있는 `team-dispatch ack` 명령을 본인의 실제 관리 terminal·owner·attempt로 실행하고 park합니다. acknowledgement는 관측 보고 수신이며 제품 업무나 큐의 완료가 아닙니다. `team-dispatch status`로 담당·이벤트·verification·heartbeat를 조회할 수 있습니다. 전송 시작 후 crash/timeout은 uncertain으로 보존하며 자동 재전송하지 않습니다. UI가 필요하면 기존 UI 검증실 요청 경로를 사용하고 실제 화면은 test-macbook의 개인 Codex 담당이 한 건씩 처리합니다.

새 옵션과 기록 형식의 단위 검사는 실제 native 계정·권한·다건 전달 완주를 증명하지 않습니다. 운영 반영 전에는 현재 소스 fingerprint, 진행 중인 배정·인계, 기존 요청을 대조하고 supervisor만 안전하게 교체해야 합니다. 팀장·제품 세션은 보존합니다.
