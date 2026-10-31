# Claude 한도 상시 수집

Claude의 기존 statusline 출력을 유지하면서 실제 응답 시각에 연결한 한도를 로컬 장부에 기록합니다. 설치 대상 프로필의 구독 로그인이 필요합니다. 회사 경로·계정·설정·백업은 공개 저장소 밖에 둡니다.

## 설정

기존 `settings.json`을 먼저 비공개 위치에 백업합니다. `statusLine`의 다른 필드와 나머지 설정은 보존하고 `command`만 다음 형식으로 감쌉니다. 예제 경로와 기존 렌더러는 환경에 맞게 바꿉니다.

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /srv/example/teamkit/teamkit_cli.py --config /srv/example/private/config.json --state /srv/example/private/state.sqlite statusline --account team --display-command 'bash /srv/example/private/original-statusline.sh'"
  }
}
```

개인 프로필은 `--account personal`을 사용합니다. 기본 Claude 홈은 `CLAUDE_CONFIG_DIR`를 비우고, 별도 홈은 해당 프로필의 환경변수로 실행합니다. 기본 홈을 명시적으로 환경변수에 넣으면 다른 인증 namespace로 취급하므로 수집을 거절합니다. 이 설정은 로그인·모델·effort·권한 모드를 변경하지 않습니다.

기존 렌더러가 없으면 `--display-command`를 생략합니다. 이때 한도 상태만 ready/pending으로 표시하며, window 누락이나 한도 초과를 성공으로 표시하지 않습니다. 기존 명령이 있다면 공백·따옴표를 포함한 전체 명령을 하나의 인자로 전달해야 합니다. 자동 생성할 때는 `shlex.join` 같은 인자 인코딩을 사용합니다. 이 값은 운영자가 승인한 shell 명령이며, statusline JSON으로부터 만들지 않습니다. 기존 렌더러는 원본 stdin bytes를 받고 stdout·종료 코드를 유지합니다. 수집 오류는 렌더러 출력을 대체하지 않습니다. 수집기 자체는 비공개 payload나 예외 내용을 출력하지 않습니다.

Claude는 설정을 다시 읽지만 원래 렌더러가 적용되는지 실제 화면으로 확인합니다. `disableAllHooks`나 관리 정책이 statusline을 금지하면 이 도구도 실행되지 않으며 우회하지 않습니다. 되돌릴 때는 현재 command가 설치한 값과 같은지 확인한 뒤 원래 statusLine만 복원하고, 이후 사용자가 편집한 다른 설정을 덮어쓰지 않습니다.

## 갱신 기준

statusline에는 한도의 서버 관찰 시각이 없습니다. 콜백 수신 시각을 사용하지 않고 **최신 main assistant 응답 기록 시각**을 유효기간의 기준으로 사용합니다.

- `session_id`와 basename이 일치하고, 해당 프로필의 `projects` 아래에 있는 정확한 `transcript_path`만 읽습니다. symlink로 범위 밖에 나가면 거절합니다.
- 파일 끝 최대 1 MiB에서 최신 완결 assistant 기록의 session·usage·timezone 있는 timestamp를 대조합니다. 본문을 해석하지 않고 원본 대화·statusline을 저장하지 않습니다.
- 최신 기록과 usage가 다르거나, 부분/대형/변경 중인 파일, 미래·만료·모호한 시각이면 수집하지 않습니다. 과거에 맞는 기록을 찾아 후퇴하지 않습니다.
- 실제 프로필 환경과 계정 일치 확인 후 정규화한 숫자·응답 hash·시각만 저장합니다. 인증 조회나 DB 대기 시간이 기준 시각을 늘리지 않습니다.
- 트랜잭션 안에서 더 최신 관측을 보존합니다. 같은 시각의 충돌은 같은 reset의 사용량 최대값을 취하고, window/reset이 다르면 unknown으로 둡니다. redraw·resume은 같은 시각을 다시 쓸 수 없으므로 TTL을 연장하지 않습니다.

이는 **response-anchor freshness**이며 upstream 한도 캐시가 관찰된 시각을 증명하지 않습니다. 같은 usage 값이 반복되면 특정 콜백과 특정 서버 응답을 유일하게 연결할 수도 없습니다. API 응답 기록을 새로 만들지 않는 한 기준 시각은 늘어나지 않는다는 계약입니다. 정밀한 서버 관찰 시각이 필요한 운영은 추가 upstream 계약이 필요합니다.

`capture --provider claude`도 같은 검증을 사용합니다. `rate_limits`만 있는 과거 수동 입력은 더 이상 배정 가능한 한도로 승격하지 않습니다. 이전 버전의 응답 근거 없는 Claude 캐시도 route에서 무시하며, 새 관측 전까지 대기합니다. `session_id`, `transcript_path`, `context_window.current_usage`를 포함한 원래 native 입력이 필요합니다. 처음 응답 전이나 오래된 세션을 재개한 직후에는 unknown이 정상입니다. 누락·만료·계정 불일치를 0%로 바꾸지 않습니다.

공식 문서상 `rate_limits`는 Pro·Max 및 일부 gateway 환경에서 제공됩니다. 회사 Team 지원은 가정하지 않으며 제공되지 않으면 새 작업 배정은 대기합니다. API나 추가 결제로 전환하지 않습니다. [Claude statusline 문서](https://code.claude.com/docs/en/statusline)

## 검증

`python3 -m unittest discover -s tests -v`는 만료 입력 replay, redraw·resume, 미래/naive timestamp, 역순 동시 콜백, 부분/대형 파일, 프로필·계정 불일치, 비밀값 비저장, 원래 렌더러의 bytes·종료 코드 보존을 검사합니다. 임시 파일·SQLite·renderer 프로세스는 실제이고 계정 조회는 대역입니다.

도입 환경에서는 새 API 응답 뒤 `route --provider claude`를 확인하고, redraw·resume 뒤에도 관측 시각이 고정되는지 및 TTL 뒤 대기하는지 확인합니다. 모델 호출 없이 초기 화면만 열었다는 사실은 한도 수집 성공이 아닙니다. 실제 요청 모델은 UI 표시와 응답 기록을 구분합니다. 특히 Haiku의 Plan Mode는 Sonnet으로 올라갈 수 있습니다. [모델 동작 문서](https://code.claude.com/docs/en/model-config)
