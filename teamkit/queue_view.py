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
          'intervention':'실행 종료 · 결과 확인 필요',
          'awaiting_verification':'검증 대기', 'awaiting_review':'리뷰 대기', 'waiting_approval':'승인 대기',
          'waiting_children':'하위 작업 대기', 'waiting_children_stop':'슬롯 반환 확인',
          'backlog':'추가 제안', 'completed':'검증 완료', 'cancelled':'정리됨', 'deferred':'보류',
          'done':'수신 기록 · 완료 미확인', 'uncertain':'결과 불명', 'waiting_handoff':'인계 확인',
          'awaiting_classification':'지시 분류 대기', 'classifying':'분류안 제출 대기',
          'classification_uncertain':'분류 전달 불명 · 재전송 금지', 'classification_unknown':'분류 결과 미확인',
          'needs_clarification':'원 지시 확인 필요', 'unknown':'상태 확인 필요'}

ACTIONS = {
    'intervention':'이전 실행의 결과와 종료 근거를 확인합니다. 새 세션에 원 업무를 중복 전송하지 마세요.',
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
        attention = phase in ('intervention','uncertain','waiting_handoff','classification_uncertain','classification_unknown','needs_clarification','unknown') or bool(item.get('reason'))
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
            and native.get('provider') == executor.get('provider')
            and (not item.get('attempt') or native.get('attempt') in (None,item['attempt'])))
        verified_working = phase == 'running' and receiver_matches and _fresh_native(config, payload) and native.get('status') == 'working'
        team = item['product']
        jobs.append(dict(id=item['id'], team=team, team_label=labels.get(team,team),
                         title=summary_title(item.get('goal', item.get('title',item['id']))), request=item.get('goal',''),
                         short_title=short_title(item.get('goal', item.get('title',item['id'])), labels.get(team,team)),
                         phase=phase, state_label='진행 중' if verified_working else LABELS.get(phase,'상태 확인 필요'), category=category,
                         reason=_reason(item.get('reason') or ''),
                         next_action=(item.get('intervention') or {}).get('next_action') or ('실행자의 작업 결과와 공식 제출 기록을 기다립니다.' if verified_working else ACTIONS.get(phase,'원 상태·실행자·결과 근거를 확인합니다. 확인 전 재전송하지 마세요.')),
                         technical_reason=item.get('reason') or '', technical_next_action=item.get('next_action',''),
                         verified_working=verified_working,
                         created_at=item.get('created_at'), updated_at=item.get('updated_at'),
                         provider=executor.get('provider'), account=executor.get('account'),
                         current_terminal=executor.get('terminal'), receiver_terminal=executor.get('terminal'),
                         receiver_matches_current=receiver_matches, source_file='영속 업무 기록 · '+item['id'],
                         task_id=item['id'], task_state=phase, origin_id=item.get('origin_id'),
                         parent_id=item.get('parent_id'), role=item.get('role'),
                         split_worktree=(item.get('worktree_split') or {}).get('worktree'),
                         result_location=(item.get('result') or {}).get('artifact'),
                         work_completion_confirmed=phase=='completed', intervention=item.get('intervention'), requires_user_decision=phase=='waiting_approval' or bool(item.get('intervention')),
                         completion_intent=phase in ('awaiting_review','awaiting_verification'),
                         execution_actions=dict(candidates='queue candidates', send='queue send',
                             ticket=item['id'], requires_candidate_preview=True,
                             send_allowed=phase=='pending' and item.get('source')!='legacy',
                             legacy=item.get('source')=='legacy',
                             reason='구형 요청이라 이 창에서 전송할 수 없습니다.' if item.get('source')=='legacy'
                                 else '' if phase=='pending' else '이미 전달되었거나 배정 가능한 대기 상태가 아닙니다. 재전송하지 마세요.')))
    for job, item in zip(jobs, payload['tickets']):
        if (item.get('source') == 'legacy' and item.get('error_code') == 'preparation_failed'
                and item.get('error_stage') in ('pending', 'waiting_handoff') and not item.get('executor')):
            job.update(state_label='전달 전 준비 실패', category='attention',
                       reason='담당자에게 전달하기 전 준비 단계에서 실패했습니다. 실제 수신 기록이 없습니다.',
                       next_action='운영본부가 현재 팀 세션·사용량과 미전달 기록을 확인한 뒤 같은 요청을 재개합니다.',
                       failure_stage=item.get('error_stage'), failure_type=item.get('error_type'))
    for job in jobs:
        job['board_state'] = board_state(state, organization_card(job))
        if job['board_state']['disposition'] == 'archived':
            job['category'] = 'archived'
    counts = Counter(j['category'] for j in jobs)
    counts['all'] = len(jobs)
    counts['active'] = sum(j['category'] not in ('history','deferred','archived') for j in jobs)
    teams = list(dict.fromkeys([*config['teams'], *config.get('operations',{}).get('departments',{}), *(x['team'] for x in jobs)]))
    lanes = [dict(team=team,label=labels.get(team,team)) for team in teams]
    value = dict(allocation_policy=payload.get('allocation_policy', 'role'), schema_version=payload['schema_version'], jobs=jobs, counts=dict(counts), lanes=lanes,
                 capacity=payload['capacity'], supervisor=payload['supervisor'], profiles=payload['profiles'],
                 candidate_slots=payload['candidate_slots'], account_profiles=payload['account_profiles'],
                 split_worktrees=payload.get('split_worktrees', []),
                 policy_ready_count=payload['ready_count'], ready_count_boundary=payload['ready_count_boundary'],
                 waiting=dict(Counter(j['state_label'] for j in jobs if j['category'] not in ('history','deferred','archived'))),
                 warnings=[], execution_actions=dict(sync='queue sync', status='queue action-status',
                     durable=True, wake_is_completion=False))
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


SECRET = re.compile(r'(?i)((?:authorization\s*:\s*bearer|(?:access[_-]?token|refresh[_-]?token|api[_-]?key|password)\s*[=:])\s*)[^\s,;]+')
SHORT_LIMIT = 40
_ID_LINE = re.compile(r'#{1,6}\s+[A-Z][A-Z0-9_-]{8,}')
_PR = re.compile(r'https?://\S+?/pulls?/(\d+)')
_RECIPIENTS = (
    re.compile(r'^(?:사용자가\s+)?\S+\s*팀장(?:에게|께)\s+(?:직접\s+)?(?:요청한\s+)?'),
    re.compile(r'^\S+\s*(?:개발팀|팀|실)(?:의)?\s+(?:\S+\s+){0,4}?담당(?:에게|께|의)\s+'),
    re.compile(r'^\S+\s*(?:개발팀|팀|실)에서\s+'),
    re.compile(r'^\S+\s*(?:개발팀|팀|실)의\s+(?:\S+\s+){0,4}?\S+로\s+'),
)
_ENDINGS = (re.compile(r'\s*(?:을|를)?\s*(?:요청|수행)(?:한다|합니다|해\s*주세요)$'),
            re.compile(r'\s*(?:(?:해|하여)\s*주세요|하세요|합니다|입니다|한다)$'))
_GENERIC = re.compile(r'(?:새\s*)?((?:읽기\s*전용\s*)?(?:검토|리뷰))')


def _team_name(label):
    name = re.sub(r'^\d+\s*·\s*', '', label or '').strip()
    return re.sub(r'\s*개발팀$', '', name) or label or ''


def _clip(text, limit=SHORT_LIMIT):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit-1].rstrip()+'…'


def short_title(text, team_label=''):
    """One short card line for narrow strips; `title` keeps the full summary."""
    raw = text if isinstance(text, str) else ''
    clean = SECRET.sub(r'\1<REDACTED>', raw)
    clean = re.sub(r'<image\b[^>]*>.*?</image>|<image\b[^>]*>|\[Image #\d+\]', '', clean, flags=re.S)
    rows = [row.strip() for row in clean.splitlines() if row.strip()]
    rows = [re.sub(r'^#{1,6}\s+', '', row) for row in rows if not _ID_LINE.fullmatch(row)]
    if not rows:
        return _clip(summary_title(clean))
    team = _team_name(team_label)
    first = rows[0].strip('[] ')
    lead = re.match(r'^((?:사용자|[^\s:：]+\s*팀장)[^:：]{0,24})[:：]\s*', first)
    if lead and re.search('요청|승인|확인', lead.group(1)):
        first = first[lead.end():]
    for pattern in _RECIPIENTS:
        first = pattern.sub('', first, count=1)
    pr = _PR.search(first)
    if pr and '리뷰' in first:
        return _clip(' '.join(filter(None, (team, f'PR #{pr.group(1)} 리뷰 정리'))))
    sentence = re.split(r'(?<=[.!?。])(?:\s|$)', first)[0].rstrip('.。 ')
    head = sentence
    for pattern in _ENDINGS:
        head = pattern.sub('', head)
    generic = _GENERIC.fullmatch(head)
    if generic or re.search(r'아래\s*(?:사용자\s*)?요청', sentence) or not head:
        target = next((row.split(':', 1)[1].strip() for row in rows[1:] if re.match(r'대상\s*[:：]', row)), '')
        kind = generic.group(1) if generic else '리뷰' if any('리뷰' in row for row in rows[:3]) else '검토'
        pr = _PR.search(target)
        if pr:
            return _clip(' '.join(filter(None, (team, f'PR #{pr.group(1)} {kind}'))))
        if target:
            return _clip(kind+' · '+target.split(' 및 ')[0].split()[0].rstrip('/').split('/')[-1])
    return _clip(head or summary_title(clean))


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
