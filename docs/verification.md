# 검증 범위와 도입 상태

| 단계 | 상태 | 근거·남은 확인 |
| --- | --- | --- |
| 공개 코드·로컬 자료 분리 | 구현 | 가짜 예제, 로컬 상태 기본 경로, 추적 파일 검사 |
| 한도 판단·프로필 바인딩 | 로컬 검증 | 90% 경계, 누락·만료·오류 값, 기대 계정 불일치 |
| 소유권·인계·동시성 | 로컬 검증 | SQLite 다중 연결, 불명 전송 재시도 금지, 체크포인트 |
| provider 실연동 | 미실행 | 최초 계정별 로그인, 공식 응답·조직 계정 제공 범위 확인 필요 |
| Herdr 실제 실행 | 미실행 | 관리 pane 안에서 startup·환경·receipt·park·tell 검증 필요 |
| 네 제품 팀 도입 | 로컬 설정 준비 후 검증 | 회사 설정은 공개 저장소에 포함하지 않음 |
| 자동 컨텍스트 교체·hook 연결 | 미구현 | 판단 함수와 수동 인계 경로만 제공 |
| 매뉴얼 실제 수정·빌드·화면 | 미실행 | 실제 문서 작업에서 검증 |
| 장기 성능·토큰 절감 | 미측정 | 유형별 비교 자료 필요 |

검사 명령은 `python3 -m unittest discover -s tests -v`입니다. policy의 주요 판단은 stub에서 동작 assertion 실패를 확인한 뒤 구현했습니다. 나머지 검사는 구현 후 추가한 회귀 검사이며, 모두 수정 전 실패를 증명한 것으로 보고하지 않습니다.

생산 경로는 CLI → 설정·계정 확인 → SQLite claim → Herdr workspace/start/prompt → 에이전트 receipt → 검증 기록입니다. 테스트에서는 실제 Git·SQLite·임시 파일을 사용하고 모델·Herdr·계정 경계는 대역으로 바꿉니다. 이 통과는 실제 모델·계정·Herdr 통합 성공을 증명하지 않습니다.

참고한 공식 계약: [Codex App Server](https://learn.chatgpt.com/docs/app-server), [Codex 인증](https://learn.chatgpt.com/docs/auth), [Claude statusline](https://code.claude.com/docs/en/statusline), [Claude 환경변수](https://code.claude.com/docs/en/env-vars). Herdr 제어 문법은 설치된 `herdr --help`, 관련 하위 명령 도움말과 `herdr --skill`을 확인합니다. 공식 계약과 설치 버전의 차이는 실제 연동 전 다시 확인합니다.
