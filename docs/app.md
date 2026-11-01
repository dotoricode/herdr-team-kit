# Herdr 앱 독립 운영

Herdr 앱은 `hc`를 사용하고 TUI는 `herdr`를 사용합니다. 기본값은 TUI입니다. 앱은 별도 설정·작업 기록·설계 경로를 명시하며, 기존 TUI 작업·소유권·승인을 복제하지 않습니다. 공통 정책과 인증 프로필은 참조할 수 있지만 실행 세션이나 계정 사용 한도가 분리되는 것은 아닙니다.

## 설정과 실행

기존 예제 설정을 저장소 밖의 GUI 전용 위치에 준비하고 `runtime_backend`를 `core`로 지정합니다. `herdr_session`과 TUI `dashboard_workspaces`는 가져오지 않습니다. `private_plan_root`도 GUI 전용 경로로 지정합니다. 작업 기록 경로는 모든 명령에 명시하세요.

```sh
python3 teamkit_cli.py --config ~/.config/herdr-team-kit-app/config.json --state ~/.local/state/herdr-team-kit-app/state.sqlite doctor
```

실행 제어는 실제 core terminal 안에서 수행합니다. `hc run --bg -- ...`로 controller를 시작하면 core가 `HC_TERMINAL_ID`를 제공합니다. 이 변수를 수동으로 만들지 않습니다. `create`, 계정 한도 admission, `dispatch`, `receipt`, `park`, 검증과 인계는 기존 절차를 따릅니다.

`dispatch TASK --terminal TERMINAL`은 이미 열린 plain leader 대화를 재시작 없이 연결합니다. core·미연결 leader·명시한 cwd/model/effort·idle/PID·GUI 작업 기록 쓰기 경로의 명시적 허용을 확인하고 기존 owner label이 있으면 거절합니다. label 전 intent를 기록하고 `receipt`가 실제 실행 프로필과 계정을 확인해야 수신 완료가 됩니다. 기존 세션에 새 sandbox나 모델 설정을 적용하는 기능이 아닙니다. TUI 작업이나 연결 설계 실행은 채택할 수 없습니다. 결과 불명은 재전송하지 않습니다.

## 운영 현황 mod

`app-mods/team-operations`를 사용자 mod 폴더로 복사하고 `local.json`을 별도로 준비합니다. 이 파일은 공개 저장소에 추가하지 않습니다.

```json
{
  "python": "/srv/example/venv/bin/python",
  "script": "/srv/example/kit/teamkit_app.py",
  "config": "/srv/example/gui/config.json",
  "state": "/srv/example/gui/state.sqlite",
  "bindings": "/srv/example/gui/bindings.json"
}
```

bindings는 `version: 1`, `actions_enabled` boolean, 설정의 모든 팀을 포함한 `teams` 매핑, 선택적인 `inventory` Markdown 경로를 받습니다. 각 팀은 `label`, 실제 `terminal` 주소, 앱 `space` ID를 갖습니다. 고정 작업 표는 `ID | 이름 | 상태 | 완료 조건` 네 칸으로 읽습니다. 경로와 실데이터는 로컬에 둡니다.

mod는 GUI 작업 기록 JSON을 조회하고 core 상태를 별도로 표시합니다. 조회는 모델을 호출하지 않으며 작업 기록를 생성하지 않습니다. 누락된 상태·한도를 idle이나 0%로 추정하지 않습니다. 제품 진행률은 기존 승인 단계의 가중치를 사용합니다. `monitor`도 GUI 설정/작업 기록를 명시해 별도로 실행합니다.

```sh
herdr-app check ~/.config/herdr-app/mods/team-operations
herdr-app status
herdr-app shot /tmp/team-operations.png
```

초기 검사만으로 비동기 상태나 실제 화면을 검증했다고 쓰지 않습니다. 기본 layout/mod를 변경할 때는 복제본을 사용합니다. `lab-rich` 등 사용자 테마는 mod의 `theme()` 색으로 따릅니다.

## 승인과 재개

설계 읽기는 plan과 contract의 범위·필수 조건·완료 단계를 함께 표시합니다. 승인은 선택한 revision/digest와 현재 파일을 트랜잭션 안에서 대조하고 명시적인 확인을 요구합니다. 승인 버튼이 구현을 자동 배정하지는 않습니다.

재개는 core 작업 기록의 동일 owner를 가진 parked leader, 또는 정지·체크포인트가 기록된 handed-off 실행만 대상으로 합니다. 확인 후 실제 core controller를 시작하며 요청 접수와 수신·실행 결과는 다릅니다. 중복·대기·결과 불명인 요청을 자동 재실행하지 않습니다. `app-actions`에 보존한 결과와 작업 기록를 먼저 대조합니다. TUI retire 경로는 core terminal을 종료하지 않습니다.

## 검증 한계

단위 검사의 subprocess 대역은 argv/상태·소유권 계약을 확인합니다. 실제 프로필·core·모델·receipt·앱 렌더는 별도 통합 검증이 필요합니다. `hc label`에는 compare-and-set이 없으므로 협력 controller의 로컬 잠금과 변경 후 identity 대조를 사용하며 외부 직접 label 변경과의 원자성을 보장하지 않습니다. 작업 기록 밖의 직접 실행 작업과 다른 실행 환경의 활성 작업은 이 admission이 통합 제어하지 않습니다.


상단 운영 바는 같은 소스를 `team-overview`라는 별도 mod 이름으로 설치하고 `props.role: "overview"`로 배치할 수 있습니다. 운영 바의 layout 높이는 최소 166px로 확보합니다. 글꼴의 실제 줄 높이와 고정 행 높이를 명시해 상태 글자의 위아래가 잘리지 않도록 합니다. 전체 현황은 `team-operations`를 사용해 명령 수신 범위를 분리합니다. `local.json`의 `dashboard_tab`, `overview_font`로 전체 현황 이동 대상과 운영 바 폰트를 지정합니다. 총괄은 GUI 복구 기록과 실제 core 소유권에서 찾으며, 최초 대상은 bindings의 `coordinator`에 둡니다. 전체 현황의 별도 입력 설정은 pane의 `props.local` 경로로 선택할 수 있습니다.

재개 요청은 선택한 owner와 attempt를 controller의 CLI까지 전달합니다. `tell --expected-owner ... --expected-attempt ...`와 `dispatch --expected-attempt ...`는 바뀐 선택을 작업 기록 변경 전에 거절합니다. core의 정상 `done`은 대기 상태로 허용합니다. 설치된 hc에는 `--if done`이 없어 이 경우 identity/상태 대조 뒤 일반 send를 사용하며, blocked를 강제로 통과시키거나 전송 실패를 반복하지 않습니다. 외부 직접 입력과의 원자성은 제공하지 않습니다.


## 총괄 창을 닫았을 때

운영 바는 연결된 총괄의 실제 core 상태를 확인합니다. 살아 있으면 같은 창으로 이동하고, 닫힌 것이 확인되면 확인을 거쳐 GUI 체크포인트를 읽는 새 총괄을 엽니다. 복구 요청·생성한 terminal·소유권·계정 확인과 체크포인트는 GUI 작업 기록 옆에 저장합니다. 진행 중이거나 결과 불명인 복구는 중복 시작하지 않습니다. core 연결 실패는 종료로 추정하지 않습니다.

설정의 `coordinator`에는 `provider: "codex"`, model/effort, GUI `private_plan_root` 안의 `root`, 지침 파일을 지정합니다. 새 총괄은 최신 구독 한도를 확인하고 GUI 기록만 읽습니다. 제품 작업은 자동 재개하지 않습니다. 종료된 대화 전문의 복원이나 core 재시작 후 쉘로 바뀐 프로그램의 자동 재실행을 보장하지 않습니다. 다른 프로그램이 남은 총괄 자리는 먼저 보존하고 확인합니다.

## 네 계정 사용량

계정별 수집 결과와 각 시간 구간을 따로 표시합니다. 5시간·주간 값에는 비례 막대와 %를 함께 표시하며 70% 미만은 녹색, 70% 이상은 주황색, 90% 이상은 빨간색입니다. 지난 기록은 회색으로 구분합니다. 이 표시 기준은 수집·배정 정책을 바꾸지 않습니다. 조회 성공·제공 안 함·조회 실패·지난 기록을 구분하며 없는 값을 0%로 표시하지 않습니다. 마지막 관측 시각은 화면 새로고침이나 로그인 확인만으로 연장하지 않습니다. Codex 공식 조회가 5시간 구간을 반환하지 않으면 해당 칸은 제공 안 함입니다.

사용량 갱신은 모델 요청 없이 공식 CLI 조회를 수행합니다. Claude는 별도 임시 CLI의 내장 `/usage`만 사용하고 자신의 임시 세션만 정리합니다. 시작 화면·계정·준비 상태를 확인하지 못하면 이유와 조치를 표시합니다. 표시용 `/usage` 관측은 작업 배정을 허용하는 응답 연계 한도로 승격하지 않습니다. `usage_probe_root`는 검증용 로컬 폴더로 지정합니다. 기존 사용자 설정이나 전역 hook을 변경하지 않습니다.

GUI Claude 실행은 프로세스별 `--settings`로 GUI statusline 수집기를 연결합니다. `claude_display_commands`에 계정별 기존 표시기를 지정할 수 있으며 TUI 수집기로 전송하는 표시기는 그대로 재사용하지 않습니다. 모니터의 주기 조회는 새 모델 대화나 반복 `/usage` 세션을 만들지 않습니다.

운영 보드는 처리할 작업을 먼저 보이고 완료된 고정 작업 목록은 펼쳐 볼 수 있습니다. 계정 상세에는 수집 출처·시각·누락 사유와 다음 조치를 표시합니다. 중요한 팀 이름은 크게, 이동 버튼과 보조 정보는 작게 표시합니다. 원래 고정 작업 ID와 미완료 항목은 접기 상태에서도 보존합니다.


## 작업 보드와 상세 조회

작업 보드는 상태별 칸반으로 조회합니다. 카드를 열면 작업 목적·담당 팀·연결된 실행과 설계를 확인할 수 있습니다. 설계는 상세 모달에서 필요할 때 읽고 닫으며 비어 있는 문서 영역을 상시 배치하지 않습니다. 조회와 창 이동은 상태 변경이나 제품 구현 승인이 아닙니다. 승인·재개는 기존 문서 버전·해시·소유권 확인을 유지합니다.

`work-detail --work ID`는 선택한 GUI 작업 기록 작업만 조회합니다. 실제 session의 owner/task/name/PID/provider를 대조한 경우에만 해당 실행으로 이동할 수 있습니다. 연결 실패는 종료로 추정하지 않고, 소유권 없는 과거 실행을 현재 작업으로 표시하지 않습니다. 카드의 모델과 effort는 요청값임을 구분합니다.

팀장의 작업 중 표시는 현재 native 상태가 working일 때만 네이티브 애니메이션으로 표시합니다. 작업 기록의 running만으로 작업 중을 추정하거나 고빈도 JavaScript 타이머로 화면을 다시 그리지 않습니다.

제목·상태·진행률·남은 작업 수 같은 짧은 UI 문구는 단어 중간에서 줄을 나누지 않습니다. 의미 단위 자식으로 나누거나 행을 쌓아 공간이 줄어도 문구 전체가 다음 행으로 이동하게 합니다. 필수 작업 수·대기 이유를 숨겨 문제를 피하지 않습니다.
