"""Display projection only. State changes remain queue/work/pool CLI actions."""
from collections import Counter
import hashlib
import json

from .config import load
from .operations import view


LABELS = {'pending':'배정 대기', 'dispatching':'배정 시작', 'received':'수신 확인', 'running':'실행 중',
          'awaiting_verification':'검증 대기', 'awaiting_review':'리뷰 대기', 'waiting_approval':'승인 대기',
          'waiting_children':'하위 작업 대기', 'waiting_children_stop':'슬롯 반환 확인',
          'backlog':'추가 제안', 'completed':'검증 완료', 'cancelled':'정리됨', 'deferred':'보류',
          'done':'수신 기록 · 완료 미확인', 'uncertain':'결과 불명', 'waiting_handoff':'인계 확인',
          'awaiting_classification':'지시 분류 대기'}


def board(config_path, state):
    config = load(config_path)
    payload = view(config_path, state)
    labels = config.get('operations', {}).get('product_labels', {})
    jobs = []
    instructions = [dict(id=x['id'], origin_id=x['id'], product='operations',
                         state='awaiting_classification', goal=x['text'], reason=x['reason'],
                         next_action=x['next_action'], created_at=x['created_at'])
                    for x in payload.get('intake_pending', [])]
    for item in [*payload['tickets'], *instructions]:
        phase = item['state']
        history = phase in ('completed','cancelled')
        attention = phase in ('uncertain','waiting_handoff') or bool(item.get('reason'))
        category = 'history' if history else 'deferred' if phase in ('deferred','backlog') else 'attention' if attention else 'progress' if phase in ('running','received','dispatching','awaiting_review','awaiting_verification') else 'waiting'
        executor = item.get('executor') or {}
        team = item['product']
        jobs.append(dict(id=item['id'], team=team, team_label=labels.get(team,team),
                         title=item.get('goal', item.get('title',item['id'])), request=item.get('goal',''),
                         phase=phase, state_label=LABELS.get(phase,phase), category=category,
                         reason=item.get('reason') or '', next_action=item.get('next_action',''),
                         created_at=item.get('created_at'), updated_at=item.get('updated_at'),
                         provider=executor.get('provider'), account=executor.get('account'),
                         current_terminal=executor.get('terminal'), receiver_terminal=executor.get('terminal'),
                         receiver_matches_current=bool(executor), source_file='영속 업무 기록 · '+item['id'],
                         task_id=item['id'], task_state=phase, origin_id=item.get('origin_id'),
                         parent_id=item.get('parent_id'), role=item.get('role'),
                         work_completion_confirmed=phase=='completed', requires_user_decision=phase=='waiting_approval',
                         completion_intent=phase in ('awaiting_review','awaiting_verification')))
    counts = Counter(j['category'] for j in jobs)
    counts['all'] = len(jobs)
    counts['active'] = sum(j['category'] not in ('history','deferred') for j in jobs)
    teams = list(dict.fromkeys([*config['teams'], *config.get('operations',{}).get('departments',{}), *(x['team'] for x in jobs)]))
    lanes = [dict(team=team,label=labels.get(team,team)) for team in teams]
    value = dict(schema_version=payload['schema_version'], jobs=jobs, counts=dict(counts), lanes=lanes,
                 capacity=payload['capacity'], supervisor=payload['supervisor'], profiles=payload['profiles'],
                 candidate_slots=payload['candidate_slots'], account_profiles=payload['account_profiles'],
                 policy_ready_count=payload['ready_count'], ready_count_boundary=payload['ready_count_boundary'],
                 waiting=dict(Counter(j['state_label'] for j in jobs if j['category'] not in ('history','deferred'))),
                 warnings=[])
    # Observation time alone never triggers rebuilding the whole view.
    semantic = dict(value)
    semantic['supervisor'] = {k:v for k,v in value['supervisor'].items() if k not in ('heartbeat_age_seconds','heartbeat')}
    semantic['capacity'] = {k:v for k,v in value['capacity'].items() if k not in ('observed_at','observation_age')}
    value['fingerprint'] = hashlib.sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return value
