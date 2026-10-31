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

현재 로컬 검사 113개가 통과했습니다. 검사 명령은 `python3 -m unittest discover -s tests -v`입니다. PR 리뷰 후에는 receipt·완료 상태·경로·리뷰 SHA·동시성 회귀 검사를 추가했습니다. policy의 주요 판단은 stub에서 동작 assertion 실패를 확인한 뒤 구현했습니다. 나머지 검사는 구현 후 추가한 회귀 검사이며, 모두 수정 전 실패를 증명한 것으로 보고하지 않습니다.

생산 경로는 CLI → 설정·계정 확인 → SQLite claim → Herdr workspace/start/prompt → 에이전트 receipt → 검증 기록입니다. 테스트에서는 실제 Git·SQLite·임시 파일을 사용하고 모델·Herdr·계정 경계는 대역으로 바꿉니다. 이 통과는 실제 모델·계정·Herdr 통합 성공을 증명하지 않습니다.

참고한 공식 계약: [Codex App Server](https://learn.chatgpt.com/docs/app-server), [Codex 인증](https://learn.chatgpt.com/docs/auth), [Claude statusline](https://code.claude.com/docs/en/statusline), [Claude 환경변수](https://code.claude.com/docs/en/env-vars). Herdr 제어 문법은 설치된 `herdr --help`, 관련 하위 명령 도움말과 `herdr --skill`을 확인합니다. 공식 계약과 설치 버전의 차이는 실제 연동 전 다시 확인합니다.

기본 Claude 홈의 인증 namespace 차이는 설치된 CLI의 모델 없는 auth status로 대조했고, 수정 후 기대 계정 대조도 확인했습니다. Codex --no-daemon은 설치 CLI 도움말과 생성 인자로 확인했으며 실제 Herdr 환경 전달 성공을 아직 증명하지는 않습니다.

Claude statusline 경로는 [수집 계약](statusline.md)의 범위로 검증합니다. 만료 입력 replay 회귀는 고정 base의 capture 경로에서 배정 가능으로 잘못 바뀌는 실패를 확인한 뒤 수정했습니다. 새 수집기는 응답 메타데이터 시각에 고정하며 서버 quota 캐시의 실제 관찰 시각을 증명하지 않습니다. 기존 renderer·파일·SQLite 경계와 live 계정/CLI 검증을 구분합니다.

워커 컨텍스트 감독은 [별도 계약](context-supervision.md)의 범위로 검사합니다. 실제 Git·SQLite·파일 잠금·임시 프로세스의 종료를 확인하고 native 관측·전송 경계 일부는 대역입니다. 실제 자동 교체·70% 통과·전체 제품 시범은 이 호스트 통과만으로 증명하지 않습니다.
