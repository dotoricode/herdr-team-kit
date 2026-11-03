"""Display projection only. State changes remain queue/work/pool CLI actions."""
from collections import Counter
import hashlib
import json
import math
import re

from .app_board import board_state, board_action

from .config import load
from .operations import view


LABELS = {'pending':'배정 대기', 'dispatching':'배정 시작', 'received':'입력 전달 확인 · 실행 수신 대기', 'running':'결과 제출 대기',
          'awaiting_verification':'검증 대기', 'awaiting_review':'리뷰 대기', 'waiting_approval':'승인 대기',
          'waiting_children':'하위 작업 대기', 'waiting_children_stop':'슬롯 반환 확인',
          'backlog':'추가 제안', 'completed':'검증 완료', 'cancelled':'정리됨', 'deferred':'보류',
          'done':'수신 기록 · 완료 미확인', 'uncertain':'결과 불명', 'waiting_handoff':'인계 확인',
          'awaiting_classification':'지시 분류 대기', 'classifying':'분류안 제출 대기',
          'classification_uncertain':'분류 전달 불명 · 재전송 금지', 'classification_unknown':'분류 결과 미확인',
          'needs_clarification':'원 지시 확인 필요', 'unknown':'상태 확인 필요'}

ACTIONS = {
    'pending':'승인·선행 작업·가용 자원을 확인한 뒤 자동 배정합니다.',
    'dispatching':'예약된 실행자와 입력 전달 결과를 확인합니다. 중복 전송하지 마세요.',
    'received':'입력 전달은 확인됐습니다. 실행자의 공식 수신 확인을 기다립니다. 재전송하지 마세요.',
    'running':'결과 제출 여부와 실행자 상태를 확인합니다. 같은 지시를 다시 보내지 마세요.',
    'awaiting_verification':'제출 결과와 해당 소스 버전의 필수 검증 근거를 확인합니다.',
    'awaiting_review':'독립 검토 결과와 연결된 수정·재검증 작업을 확인합니다.',
    'waiting_approval':'원 지시·변경 경로·계정·완료 조건을 대조해 승인 범위를 확인해 주세요.',
    'waiting_children':'필수 하위 작업의 완료를 기다린 뒤 그 결과로 원 업무를 이어갑니다.',
    'waiting_children_stop':'현재 실행과 쓰기 중단을 확인한 뒤 실행 슬롯 반환을 확인합니다.',
    'backlog':'원 승인 범위를 넘는 추가 제안입니다. 별도 승인 전에는 실행하지 않습니다.',
    'completed':'검증 완료 기록을 보존하고 선행 조건이 풀린 다음 업무의 배정을 확인합니다.',
    'cancelled':'정리 근거와 원 기록을 보존합니다.',
    'deferred':'보류 이유를 보존하고 재개 조건 또는 사용자 지시를 기다립니다.',
    'done':'수신·실행·결과 근거를 대조해 원 업무의 완료 여부를 확인합니다. 재전송하지 마세요.',
    'uncertain':'원 시도·실행자·수신·결과를 대조합니다. 확인 전 재전송하지 마세요.',
    'waiting_handoff':'원 실행자의 중단·소유권·수신 기록을 대조한 뒤 인계를 이어갑니다.',
    'awaiting_classification':'기존 총괄의 실제 대기 상태·입력·사용량을 확인한 뒤 지시를 분류합니다.',
    'classifying':'기존 총괄의 분류 제출을 기다립니다. 같은 원 지시를 재전송하지 마세요.',
    'classification_uncertain':'원 분류 시도와 총괄의 수신·제출 기록을 대조합니다. 재전송하지 마세요.',
    'classification_unknown':'원 분류 시도와 총괄의 제출 기록을 확인합니다. 재전송하지 마세요.',
    'needs_clarification':'원 지시의 모호한 목표·범위·승인 조건을 운영본부에서 확인해 주세요.',
}


def _reason(raw):
    if not raw or any('\uac00' <= ch <= '\ud7a3' for ch in raw):
        return raw
    text = raw.lower()
    for terms, label in (
        (('approval', 'grant'), '승인 근거가 없거나 바뀌었습니다. 원 지시와 승인 범위를 확인해 주세요.'),
        (('quota', 'usage', 'profile'), '계정 사용량·인증·관측 유효기간을 확인한 뒤 배정합니다.'),
        (('capacity', 'slot', 'resource'), '가용 실행 슬롯 또는 충돌 자원의 반환을 기다립니다.'),
        (('dependency', 'children'), '필수 선행·하위 작업의 검증 완료를 기다립니다.'),
        (('owner', 'pid', 'identity'), '실행자 신원·프로세스·소유권이 바뀌었는지 대조해야 합니다.'),
        (('draft', 'input', 'idle'), '실행자의 작업·미전송 입력·입력 가능 상태를 확인해야 합니다.'),
        (('unknown', 'uncertain'), '원 전달의 결과를 확인하지 못했습니다. 원 기록을 대조하고 재전송하지 마세요.'),
    ):
        if any(term in text for term in terms):
            return label
    return '현재 대기 이유의 원 기록을 상세에서 대조해 주세요.'


def _fresh_native(config, payload):
    observation = payload.get('native_observation') or {}
    stamps = (payload.get('observed_at'), observation.get('observed_at'))
    if (observation.get('complete') is not True
            or not all(type(x) in (int, float) and math.isfinite(x) for x in stamps)):
        return False
    ttl = config.get('operations', {}).get('resource_policy', {}).get('observation_ttl_seconds', 15)
    return 0 <= stamps[0] - stamps[1] <= ttl


def board(config_path, state):
    config = load(config_path)
    payload = view(config_path, state)
    labels = config.get('operations', {}).get('product_labels', {})
    jobs = []
    # Captured conversation is an inbox, not an approved work ticket.
    for item in payload['tickets']:
        phase = item['state']
        history = phase in ('completed','cancelled')
        attention = phase in ('uncertain','waiting_handoff','classification_uncertain','classification_unknown','needs_clarification','unknown') or bool(item.get('reason'))
        category = 'history' if history else 'deferred' if phase in ('deferred','backlog') else 'attention' if attention else 'progress' if phase in ('running','received','dispatching','awaiting_review','awaiting_verification') else 'waiting'
        executor = item.get('executor') or {}
        native = next((x for x in (payload.get('native_observation') or {}).get('terminals', [])
                       if x.get('terminal') == executor.get('terminal')), {})
        actor = next((x for x in payload.get('task_actors', []) if x.get('id') == executor.get('task')), {})
        runtime = actor.get('runtime') or {}
        receiver_matches = bool(executor and actor
            and actor.get('owner') == executor.get('owner')
            and runtime.get('terminal') == executor.get('terminal')
            and runtime.get('native_pid') == executor.get('pid')
            and native.get('pid') == executor.get('pid')
            and native.get('owner') == executor.get('owner')
            and native.get('provider') == executor.get('provider'))
        verified_working = phase == 'running' and receiver_matches and _fresh_native(config, payload) and native.get('status') == 'working'
        team = item['product']
        jobs.append(dict(id=item['id'], team=team, team_label=labels.get(team,team),
                         title=summary_title(item.get('goal', item.get('title',item['id']))), request=item.get('goal',''),
                         phase=phase, state_label='진행 중' if verified_working else LABELS.get(phase,'상태 확인 필요'), category=category,
                         reason=_reason(item.get('reason') or ''),
                         next_action='실행자의 작업 결과와 공식 제출 기록을 기다립니다.' if verified_working else ACTIONS.get(phase,'원 상태·실행자·결과 근거를 확인합니다. 확인 전 재전송하지 마세요.'),
                         technical_reason=item.get('reason') or '', technical_next_action=item.get('next_action',''),
                         verified_working=verified_working,
                         created_at=item.get('created_at'), updated_at=item.get('updated_at'),
                         provider=executor.get('provider'), account=executor.get('account'),
                         current_terminal=executor.get('terminal'), receiver_terminal=executor.get('terminal'),
                         receiver_matches_current=receiver_matches, source_file='영속 업무 기록 · '+item['id'],
                         task_id=item['id'], task_state=phase, origin_id=item.get('origin_id'),
                         parent_id=item.get('parent_id'), role=item.get('role'),
                         work_completion_confirmed=phase=='completed', requires_user_decision=phase=='waiting_approval',
                         completion_intent=phase in ('awaiting_review','awaiting_verification')))
    for job in jobs:
        job['board_state'] = board_state(state, organization_card(job))
        if job['board_state']['disposition'] == 'archived':
            job['category'] = 'archived'
    counts = Counter(j['category'] for j in jobs)
    counts['all'] = len(jobs)
    counts['active'] = sum(j['category'] not in ('history','deferred','archived') for j in jobs)
    teams = list(dict.fromkeys([*config['teams'], *config.get('operations',{}).get('departments',{}), *(x['team'] for x in jobs)]))
    lanes = [dict(team=team,label=labels.get(team,team)) for team in teams]
    value = dict(schema_version=payload['schema_version'], jobs=jobs, counts=dict(counts), lanes=lanes,
                 capacity=payload['capacity'], supervisor=payload['supervisor'], profiles=payload['profiles'],
                 candidate_slots=payload['candidate_slots'], account_profiles=payload['account_profiles'],
                 policy_ready_count=payload['ready_count'], ready_count_boundary=payload['ready_count_boundary'],
                 waiting=dict(Counter(j['state_label'] for j in jobs if j['category'] not in ('history','deferred','archived'))),
                 warnings=[])
    # Observation time alone never triggers rebuilding the whole view.
    semantic = dict(value)
    semantic['supervisor'] = {k:v for k,v in value['supervisor'].items() if k not in ('heartbeat_age_seconds','heartbeat')}
    dispatch = semantic['supervisor'].get('dispatch')
    if isinstance(dispatch,dict):
        dispatch={k:v for k,v in dispatch.items() if k not in ('observed_at','heartbeat_age_seconds','last_success_at','consecutive_errors')}
        if isinstance(dispatch.get('last_error'),dict):
            dispatch['last_error']={k:v for k,v in dispatch['last_error'].items() if k!='at'}
        semantic['supervisor']['dispatch']=dispatch
    semantic['capacity'] = {k:v for k,v in value['capacity'].items() if k not in ('observed_at','observation_age')}
    value['fingerprint'] = hashlib.sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return value


def summary_title(text):
    """Hide transport envelopes only in the one-line card summary."""
    text = re.sub(r'<image\b[^>]*>.*?</image>', '', text, flags=re.S)
    text = re.sub(r'<image\b[^>]*>', '', text)
    text = re.sub(r'\[Image #\d+\]', '', text)
    return ' '.join(text.split()) or '첨부된 요청'


def organization_card(job):
    # Dedicated namespace reuses the reversible journal without expanding
    # feedback/approval card kinds or conflating this with an existing card.
    return dict(kind='inventory', id='queue-'+hashlib.sha256(job['id'].encode()).hexdigest(),
                title=job['title'], prompt=job['request'], condition=None)


def organize(config_path, state, ident, action, client_request_id, expected_revision):
    job = next((job for job in board(config_path, state)['jobs'] if job['id'] == ident), None)
    if job is None:
        raise ValueError('현재 업무 카드를 찾을 수 없습니다. 최신 목록을 확인하세요.')
    return board_action(state, organization_card(job), action, client_request_id, expected_revision)
