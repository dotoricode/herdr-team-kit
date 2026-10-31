# 워커 컨텍스트 감독

`context_supervision: true`로 새로 배정하는 구현·리뷰 워커에만 감독기를 선택 적용합니다. 기본값은 false입니다. 기존 팀장·세션·전역 설정은 바꾸거나 종료하지 않습니다. 구현은 Unix의 파일 잠금·프로세스 조회를 사용합니다.

새 managed Codex 워커는 실행 인자로 native `context-remaining` footer를 표시합니다. 마지막 footer의 실제 남은 비율을 사용률로 변환합니다. 표시가 없거나 지원하지 않으면 unknown입니다. Claude는 이 footer 경로를 지원하지 않으므로 unknown이며 임의 사용률을 만들지 않습니다. 이 기능은 quota 유효기간을 바꾸지 않습니다. [공식 Codex 설정](https://learn.chatgpt.com/docs/config-file/config-sample)을 참고합니다.

## 안전 경계

워커는 완료한 검증 단위 뒤에 작업이 남아 있다면 다음 JSON을 장부 디렉터리의 로컬 파일에 작성합니다. 전체 대화나 비밀값을 넣지 않습니다.

```json
{"verification":["command, result and private evidence location"],"next":"next bounded action"}
```

```bash
python3 teamkit_cli.py boundary TASK-1 --owner OWNER --evidence-file /srv/example/private/boundary.json --rotate-if-unknown
```

실패한 시도라면 기존 routing의 `failure` 원인·종류를 추가할 수 있습니다. 경계 기록은 실제 HEAD·dirty·worktree fingerprint와 현재 receipted owner에 고정합니다. 입력 파일로 이를 덮어쓸 수 없습니다. 기록 뒤에는 추가 수정 없이 턴을 끝냅니다. 전체 작업을 마쳤다면 boundary 대신 기존 complete로 실제 완료 근거를 기록합니다.

사용률 60% 이상은 prepare, 70% 이상은 경계 확인 후 교체 대상입니다. unknown일 때 `--rotate-if-unknown`을 명시한 경계는 새 세션으로 교체합니다. 알려진 사용률이 낮으면 경계를 한 번 소비하고 같은 소유자·세션에서 다음 작업을 계속하도록 전송합니다. 실패 경계는 사용률과 관계없이 종료 확인 후 인계해 실패 이력을 보존합니다. 환경 실패는 추가 모델 시도 없이 대기합니다.

## 감독과 복구

```bash
python3 teamkit_cli.py supervise TASK-1 --owner OWNER
python3 teamkit_cli.py supervise TASK-1 --owner OWNER --apply
python3 teamkit_cli.py watch TASK-1 --owner OWNER --interval 5 --max-seconds 86400
```

supervise 기본값은 native 관찰·판단만 수행하며 입력 전송·소유권 이전을 하지 않습니다. apply/watch는 실제 Herdr 관리 환경과 opt-in이 필요합니다. dispatch는 opt-in 워커에 watch를 별도 프로세스로 시작하며 native 모델 턴을 상태 확인용으로 만들지 않습니다. watch는 소유자 변경·작업 종료·설정 비활성화·확인 필요 상태에서 끝납니다. 관측·일반 CLI 실행·모델 전송 완료는 서로 다릅니다.

교체는 owner·native agent 이름/provider/pane·idle/done 상태·명시적 경계·현재 Git 상태를 대조한 뒤에만 진행합니다. 먼저 stopping 의도를 장부에 기록하고 해당 native CLI에 한 번만 /quit 또는 /exit를 전송합니다. shell foreground 복귀, 기존 프로세스와 알려진 자식 및 프로세스 그룹 종료를 확인한 뒤 Git을 다시 대조하고 handoff합니다. working·blocked·다른 프로그램·프로세스 잔존·Git 변경은 새 소유권으로 넘기지 않습니다. 외부에서 독립적으로 시작한 모든 쓰기 프로세스를 탐지하는 도구는 아니므로 워커가 백그라운드 쓰기를 남기지 않는 안전 경계를 선언해야 합니다.

그 뒤 새 dispatch가 기존 quota·계정·모델·HEAD·fingerprint·소유권 검사를 다시 수행합니다. Codex 한도는 구독 native 읽기로 회사부터 확인하며 Claude는 실제 수집 캐시를 사용합니다. 사용 불가면 handed_off 상태로 대기하고 API·추가 결제·다른 계열 모델로 우회하지 않습니다. 회사 계정 복귀는 새 dispatch에만 적용됩니다.

전송·종료 결과가 불명확하면 자동 재전송하지 않습니다. stopping은 활성 소유권을 유지합니다. 이미 종료된 writer와 동일한 경계를 확인할 수 있으면 supervise apply로 남은 인계를 마칠 수 있지만, writer가 아직 있으면 inspect로 남깁니다. 계속 전송의 불확실 상태도 수동 확인이 필요합니다. 운영자가 native 상태·실제 파일을 확인한 뒤 기존 인계 절차로 복구하며, 승인·검증 근거를 만들어내지 않습니다. 사용자 결정이 필요한 high 실패는 기존 routing의 ask_user를 유지합니다.

## 검증 범위

검사는 실제 Git·SQLite·파일 잠금·임시 프로세스를 사용합니다. native Herdr의 관측·전송과 종료 확인 일부는 대역이며, 실제 모델의 경계 생성·70% 통과·계정 전환·자동 새 세션은 별도 native 검증이 필요합니다. 호스트 검사 통과만으로 제품의 전체 자동 인계 완료를 주장하지 않습니다.
