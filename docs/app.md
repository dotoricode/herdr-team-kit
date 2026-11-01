# Herdr 앱 독립 운영

Herdr 앱은 `hc`를 사용하고 TUI는 `herdr`를 사용합니다. 기본값은 TUI입니다. 앱은 별도 설정·장부·설계 경로를 명시하며, 기존 TUI 작업·소유권·승인을 복제하지 않습니다. 공통 정책과 인증 프로필은 참조할 수 있지만 실행 세션이나 계정 사용 한도가 분리되는 것은 아닙니다.

## 설정과 실행

기존 예제 설정을 저장소 밖의 GUI 전용 위치에 준비하고 `runtime_backend`를 `core`로 지정합니다. `herdr_session`과 TUI `dashboard_workspaces`는 가져오지 않습니다. `private_plan_root`도 GUI 전용 경로로 지정합니다. 장부 경로는 모든 명령에 명시하세요.

```sh
python3 teamkit_cli.py --config ~/.config/herdr-team-kit-app/config.json --state ~/.local/state/herdr-team-kit-app/state.sqlite doctor
```

실행 제어는 실제 core terminal 안에서 수행합니다. `hc run --bg -- ...`로 controller를 시작하면 core가 `HC_TERMINAL_ID`를 제공합니다. 이 변수를 수동으로 만들지 않습니다. `create`, 계정 한도 admission, `dispatch`, `receipt`, `park`, 검증과 인계는 기존 절차를 따릅니다.

`dispatch TASK --terminal TERMINAL`은 이미 열린 plain leader 대화를 재시작 없이 연결합니다. core·미연결 leader·명시한 cwd/model/effort·idle/PID를 확인하고 기존 owner label이 있으면 거절합니다. label 전 intent를 기록하고 `receipt`가 실제 실행 프로필과 계정을 확인해야 수신 완료가 됩니다. 기존 세션에 새 sandbox나 모델 설정을 적용하는 기능이 아닙니다. TUI 작업이나 연결 설계 실행은 채택할 수 없습니다. 결과 불명은 재전송하지 않습니다.

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

mod는 GUI 장부 JSON을 조회하고 core 상태를 별도로 표시합니다. 조회는 모델을 호출하지 않으며 장부를 생성하지 않습니다. 누락된 상태·한도를 idle이나 0%로 추정하지 않습니다. 제품 진행률은 기존 승인 단계의 가중치를 사용합니다. `monitor`도 GUI 설정/장부를 명시해 별도로 실행합니다.

```sh
herdr-app check ~/.config/herdr-app/mods/team-operations
herdr-app status
herdr-app shot /tmp/team-operations.png
```

초기 검사만으로 비동기 상태나 실제 화면을 검증했다고 쓰지 않습니다. 기본 layout/mod를 변경할 때는 복제본을 사용합니다. `lab-rich` 등 사용자 테마는 mod의 `theme()` 색으로 따릅니다.

## 승인과 재개

설계 읽기는 plan과 contract의 범위·필수 조건·완료 단계를 함께 표시합니다. 승인은 선택한 revision/digest와 현재 파일을 트랜잭션 안에서 대조하고 명시적인 확인을 요구합니다. 승인 버튼이 구현을 자동 배정하지는 않습니다.

재개는 core 장부의 동일 owner를 가진 parked leader, 또는 정지·체크포인트가 기록된 handed-off 실행만 대상으로 합니다. 확인 후 실제 core controller를 시작하며 요청 접수와 수신·실행 결과는 다릅니다. 중복·대기·결과 불명인 요청을 자동 재실행하지 않습니다. `app-actions`에 보존한 결과와 장부를 먼저 대조합니다. TUI retire 경로는 core terminal을 종료하지 않습니다.

## 검증 한계

단위 검사의 subprocess 대역은 argv/상태·소유권 계약을 확인합니다. 실제 프로필·core·모델·receipt·앱 렌더는 별도 통합 검증이 필요합니다. `hc label`에는 compare-and-set이 없으므로 협력 controller의 로컬 잠금과 변경 후 identity 대조를 사용하며 외부 직접 label 변경과의 원자성을 보장하지 않습니다. 장부 밖의 직접 실행 작업과 다른 실행 환경의 활성 작업은 이 admission이 통합 제어하지 않습니다.
