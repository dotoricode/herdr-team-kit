"""Durable UI requests consumed by the existing sequential dispatch supervisor.

A wake is a hint, never a delivery receipt. Claimed actions are never reclaimed:
inspect their original attempt after a consumer crash instead of retrying a send.
"""
import json
import re
import sqlite3
import time

from .config import load
from .operations import NativeRuntime, enabled, policy, tick
from .tickets import TicketStore, digest


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS queue_actions
                  (id TEXT PRIMARY KEY, body TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS queue_action_holds
                  (ticket TEXT PRIMARY KEY, action TEXT NOT NULL, candidate TEXT NOT NULL)''')


def executor_identity(executor):
    if executor.get('new') is True:
        from .workers import PLAN_FIELDS
        return digest({key: executor.get(key) for key in ('new', *PLAN_FIELDS)})
    # Display aliases never participate in identity.
    return digest(executor)


def selection_hold(db, ticket):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_action_holds'").fetchone():
        row = db.execute('SELECT action,candidate FROM queue_action_holds WHERE ticket=?', (ticket,)).fetchone()
        if row:
            return dict(action=row[0], candidate=row[1])
    # Earlier durable send records also express a manual selection. Upgrading
    # must not turn an existing blocked/queued action back into automatic work.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_actions'").fetchone():
        for row in db.execute('SELECT body FROM queue_actions ORDER BY rowid DESC'):
            action = json.loads(row[0])
            if action['kind'] == 'send' and action['request']['ticket'] == ticket:
                return dict(action=action['id'], candidate=action['request']['candidate_id'])
    return None


def reserve_selection(db, ticket, executor, action_id=None):
    """Called inside the official reserve transaction, including automatic ticks."""
    hold = selection_hold(db, ticket)
    if hold is None:
        if action_id is not None:
            raise ValueError('manual selection hold missing; no input sent')
        return
    if (hold['action'] != action_id or hold['candidate'] != executor_identity(executor)
            or _read(db, hold['action'])['status'] != 'running'):
        raise ValueError('manual executor selection held; explicit fresh selection required, no fallback')


def _previous_selection_reason(db, ticket):
    hold = selection_hold(db, ticket)
    if hold and _read(db, hold['action'])['status'] in ('queued', 'running', 'uncertain'):
        return 'previous selected action is queued, running or uncertain; inspect original outcome before reselection'
    return None


def _send_reason(queue, ticket, config):
    if not enabled(config):
        return 'new work is disabled by current operations policy'
    if ticket['state'] != 'pending':
        return 'ticket already assigned, uncertain, completed or not pending; never resend'
    if ticket['role'] == 'ui':
        return 'UI work requires the existing UI lab'
    return queue.gate(ticket['id'])


# Known gate messages -> short Korean phrases. Unknown text keeps its original wording.
REASON_KO = (
    ('executor responsibility or role mismatch', '담당 제품·역할이 이 업무와 맞지 않습니다'),
    ('executor draft, dialog or incomplete screen', '화면에 입력 중인 내용이나 확인 창이 있습니다'),
    ('global execution capacity is exhausted', '전체 동시 실행 용량이 가득 찼습니다'),
    ('profile execution capacity is exhausted or unknown', '계정 동시 실행 용량이 가득 찼거나 확인되지 않았습니다'),
    ('Core terminal owner/process changed', '터미널의 소유자나 프로세스가 바뀌었거나 종료되었습니다'),
    ('registered executor PID changed', '등록된 직원 프로세스가 바뀌었습니다'),
    ('Core agent is not idle and ready', '직원이 작업 중이거나 준비되지 않았습니다'),
    ('profile quota unavailable', '계정 사용량을 확인할 수 없거나 한도에 도달했습니다'),
    ('profile quota changed before delivery', '전송 직전 계정 사용량이 바뀌었습니다'),
    ('executor model/effort differs', '직원의 모델·추론 강도가 역할 정책과 다릅니다'),
    ('executor provider is outside', '직원의 계정이 역할 정책 범위 밖입니다'),
    ('executor profile binding changed', '직원의 계정 연결이 바뀌었습니다'),
    ('executor working boundary', '직원의 작업 폴더가 업무 범위를 덮지 않습니다'),
    ('review executor must be independent', '리뷰는 구현 담당과 다른 직원이어야 합니다'),
    ('no explicit responsibility/role policy', '이 업무의 역할 정책이 정해져 있지 않습니다'),
    ('role policy requires a user decision', '역할 정책상 사용자 결정이 필요합니다'),
    ('planned role/profile/quota no longer eligible', '새로 만들 직원 계획이 더 이상 유효하지 않습니다'),
    ('executor identity/profile mismatch', '직원 식별 정보나 계정이 업무 조건과 맞지 않습니다'),
    ('previous selected action is queued, running or uncertain', '이전에 선택한 전송이 진행 중이거나 결과가 불명확해 먼저 확인이 필요합니다'),
    ('native terminal already belongs to another live attempt', '이 직원은 다른 작업을 진행 중입니다'),
    ('another attempt holds a conflicting resource lease', '다른 작업이 같은 자원을 쓰고 있어 충돌합니다'),
    ('origin execution budget exhausted', '이 업무의 실행 횟수·토큰 한도를 모두 썼습니다'),
    ('personal document staff', '개인 문서 담당 규칙에 맞지 않습니다'),
    ('staff department does not support this role', '이 부서는 해당 역할을 지원하지 않습니다'),
    ('UI work requires', 'UI 업무는 UI 검증실을 통해서만 처리할 수 있습니다'),
    ('pinned product design approval', '제품 설계 승인이 없거나 바뀌었습니다'),
    ('code/integration ticket requires', '코드 업무는 고정된 승인 기록이 필요합니다'),
    ('approval missing or changed', '승인 기록이 없거나 바뀌었습니다'),
    ('ticket already assigned', '이미 배정·완료되었거나 대기 상태가 아니라 다시 보낼 수 없습니다'),
    ('new work is disabled', '현재 운영 정책에서 새 업무 전송이 꺼져 있습니다'),
    ('previous start result unknown', '이전 시작 결과 불명 — 직원 터미널이 시작할 때 만든 프로세스와 같은지 확인되지 않았습니다'),
    ('unregistered terminal in team space', '팀 스페이스에 있지만 직원으로 등록되지 않은 터미널이라 업무를 보낼 수 없습니다 (등록 필요)'),
    ('no eligible registered executor', '배정 가능한 등록 직원이 없습니다'),
)
_OVERRIDE_REASON_MAX = 300


def reason_ko(reason):
    """Short Korean label for a gate message; unknown messages keep the original text."""
    if not reason:
        return ''
    for needle, korean in REASON_KO:
        if needle in reason:
            return korean
    return '배정 불가 사유(원문): ' + reason


def _clean_override_reason(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _OVERRIDE_REASON_MAX:
        raise ValueError('override reason required: 1..%d characters' % _OVERRIDE_REASON_MAX)
    return value.strip()


def candidates(config_path, state, ident, runtime=None):
    config = load(config_path)
    runtime = runtime or NativeRuntime(config, state)
    queue = TicketStore(state, read_only=True)
    try:
        ticket = queue.get(ident)
        reason = _send_reason(queue, ticket, config) or _previous_selection_reason(queue.db, ident)
        rows = []
        if reason:
            return dict(ticket=ident, expected_ticket=digest(ticket), candidates=[], send_allowed=False, reason=reason)
        try:
            executors = runtime.executors(ticket)
        except Exception as error:
            return dict(ticket=ident, expected_ticket=digest(ticket), candidates=[], send_allowed=False, reason=str(error))
        roster = getattr(runtime, 'roster', None)
        view = roster(ticket, executors) if roster else dict(info={}, unregistered=[])
        info, excluded = view.get('info', {}), []
        for executor in executors:
            seen = info.get(executor.get('terminal'), {})
            if executor.get('new') is not True and seen.get('state') not in (None, 'live'):
                # A finished or removed terminal is not staff; keep why it was left out.
                excluded.append(dict(terminal=executor['terminal'], label=seen.get('label'), state=seen['state'],
                                     reason_ko='터미널이 종료되었거나 사라져 후보에서 제외했습니다'))
                continue
            problem = None
            try:
                runtime.admit(ticket, executor)
                # Run the official reservation gates against a private SQLite copy.
                # This preview cannot hold resources, spend budget or alter a ticket.
                preview = object.__new__(TicketStore)
                preview.db = sqlite3.connect(':memory:', isolation_level=None)
                preview.path, preview.clock = queue.path, time.time
                try:
                    queue.db.backup(preview.db)
                    # Candidate previews may offer a fresh explicit reselection;
                    # only the real reserve transaction enforces a current hold.
                    preview.db.execute('DROP TABLE IF EXISTS queue_action_holds')
                    preview.db.execute('DROP TABLE IF EXISTS queue_actions')
                    from .admission import configure
                    configure(preview.db, policy(config))
                    preview.reserve(ident, executor, policy(config), runtime.observation(), expected_revision=digest(ticket))
                finally:
                    preview.close()
            except Exception as error:
                problem = str(error)
            from .staff import display_name
            generated = display_name(executor['provider'], executor['account'], product=ticket['product'],
                    role=ticket['role'], owner=None if executor.get('new') else executor.get('owner'),
                    aliases=config.get('operations', {}).get('staff_aliases'))
            rows.append(dict(id=executor_identity(executor),
                # Same source as the sidebar: the terminal's own display label, then the generated alias.
                alias=seen.get('label') or generated, name_source='label' if seen.get('label') else 'generated',
                terminal=executor.get('terminal'),
                other_team=executor.get('new') is not True and executor.get('product') not in (ticket['product'], '*'),
                # Background temporary workers are not part of the visible team roster.
                hidden_by_default=(executor.get('new') is not True and executor.get('product') not in (ticket['product'], '*'))
                    or seen.get('background') is True,
                account=executor['account'], provider=executor['provider'], role=ticket['role'],
                eligible=problem is None, reason=problem or '', reason_ko=reason_ko(problem),
                # Manual priority never creates a worker beyond capacity; only an existing terminal can be forced.
                override_allowed=problem is not None and executor.get('new') is not True,
                planned=executor.get('new') is True))
        for item in view.get('unregistered', []):
            rows.append(dict(id=digest(dict(unregistered=item['terminal'], ticket=ident)),
                alias=item.get('label') or '이름 미확인', name_source='label' if item.get('label') else 'generated',
                terminal=item['terminal'], other_team=False, hidden_by_default=False, account=None, provider=None, role=ticket['role'],
                eligible=False, reason='unregistered terminal in team space',
                reason_ko=reason_ko('unregistered terminal in team space'), override_allowed=False,
                planned=False, unregistered=True))
        return dict(ticket=ident, expected_ticket=digest(ticket), candidates=rows, excluded=excluded,
                    send_allowed=any(row['eligible'] for row in rows),
                    reason='' if any(row['eligible'] for row in rows) else 'no eligible registered executor or approved worker plan')
    finally:
        queue.close()


def _read(db, ident):
    row = db.execute('SELECT body FROM queue_actions WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('unknown queue action')
    return json.loads(row[0])


def status(state, ident):
    queue = TicketStore(state, read_only=True)
    try:
        if not queue.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_actions'").fetchone():
            raise ValueError('unknown queue action')
        return _read(queue.db, ident)
    finally:
        queue.close()


def _save(db, action):
    db.execute('INSERT OR REPLACE INTO queue_actions VALUES (?,?)', (action['id'], json.dumps(action)))


def _identity_request(request):
    """What a client-request-id binds. The preview-time reasons are a record, not part of the request."""
    override = request.get('override')
    if not isinstance(override, dict):
        return request
    return {**request, 'override': {key: value for key, value in override.items() if key != 'original_reasons'}}


def _recorded(state, client_request_id, request):
    queue = TicketStore(state, read_only=True)
    try:
        if not queue.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_actions'").fetchone():
            return None
        row = queue.db.execute('SELECT body FROM queue_actions WHERE id=?', (client_request_id,)).fetchone()
    finally:
        queue.close()
    if row is None:
        return None
    result = json.loads(row[0])
    if _identity_request(result['request']) != _identity_request(request):
        raise ValueError('client-request-id already binds another immutable action')
    return result


def enqueue(config_path, state, kind, client_request_id, *, ticket=None, candidate_id=None, expected_ticket=None,
            override=False, override_reason=None, runtime=None):
    if not isinstance(client_request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', client_request_id):
        raise ValueError('bounded stable client-request-id required')
    if kind not in ('send', 'sync'):
        raise ValueError('unknown queue action')
    if kind == 'send' and (not ticket or any(not isinstance(x, str) or not re.fullmatch('[0-9a-f]{64}', x)
                                           for x in (candidate_id, expected_ticket))):
        raise ValueError('ticket, exact candidate-id and expected-ticket required')
    request = dict(kind=kind, ticket=ticket, candidate_id=candidate_id, expected_ticket=expected_ticket)
    if override is not True and override_reason not in (None, ''):
        raise ValueError('override reason requires the explicit override option')
    if override is True:
        if kind != 'send':
            raise ValueError('manual priority applies only to a send action')
        request['override'] = dict(reason=_clean_override_reason(override_reason))
        # A retry of an already recorded request returns that action before any new preview:
        # the preview itself changes once the first request holds the ticket.
        known = _recorded(state, client_request_id, request)
        if known is not None:
            return known
        # Preview the gates now so the durable record keeps why this was not assignable.
        chosen = next((row for row in candidates(config_path, state, ticket, runtime=runtime)['candidates']
                       if row['id'] == candidate_id), None)
        if chosen is not None and chosen.get('planned'):
            raise ValueError('planned worker cannot be sent by manual priority')
        request['override']['original_reasons'] = [chosen['reason']] if chosen and chosen['reason'] else []
    queue = TicketStore(state)
    try:
        schema(queue.db)
        with queue.transaction():
            row = queue.db.execute('SELECT body FROM queue_actions WHERE id=?', (client_request_id,)).fetchone()
            if row:
                result = json.loads(row[0])
                if _identity_request(result['request']) != _identity_request(request):
                    raise ValueError('client-request-id already binds another immutable action')
                return result
            result = dict(id=client_request_id, kind=kind, request=request, status='queued',
                          reason='durable request recorded; waiting for dispatch consumer', created_at=time.time(), result=None)
            if kind == 'send':
                current = queue.get(ticket)
                previous = _previous_selection_reason(queue.db, ticket)
                problem = (_send_reason(queue, current, load(config_path)) or previous)
                if digest(current) != expected_ticket:
                    problem = 'ticket changed before manual selection; refresh candidates'
                if current.get('attempt'):
                    problem = 'existing execution attempt requires original outcome inspection; never resend'
                # Even a stale preview expresses a manual selection. Preserve
                # that intent on pending work, but never replace a live action.
                if current['state']=='pending' and not current.get('attempt') and not previous:
                    queue.db.execute('INSERT OR REPLACE INTO queue_action_holds VALUES (?,?,?)',
                                     (ticket, client_request_id, candidate_id))
                if problem:
                    result.update(status='blocked', reason=problem, finished_at=time.time())
            _save(queue.db, result)
        from .wake import notify
        # Do not hold SQLite while waking the existing consumer.
        result['wake_accepted'] = notify(state)
        return result
    finally:
        queue.close()


def consume(config_path, state, runtime=None):
    """CAS-claim queued requests; a second consumer never repeats a claimed action."""
    queue = TicketStore(state)
    try:
        schema(queue.db)
        pending = [row[0] for row in queue.db.execute("SELECT id FROM queue_actions ORDER BY rowid")]
        results = []
        for ident in pending:
            with queue.transaction():
                action = _read(queue.db, ident)
                if action['status'] != 'queued':
                    continue
                action.update(status='running', started_at=time.time(), reason='dispatch consumer processing original action')
                _save(queue.db, action)
            try:
                before = {x['id']: x for x in queue.list()}
                forced = None
                request = action['request']
                if action['kind'] == 'send':
                    ticket = queue.get(request['ticket'])
                    problem = _send_reason(queue, ticket, load(config_path))
                    if digest(ticket) != request['expected_ticket']:
                        problem = 'ticket changed since candidate preview; refresh candidates'
                    if problem:
                        raise ValueError(problem)
                    dispatch = tick(config_path, state, runtime=runtime, selection={**request, 'action_id':action['id']})
                    forced = next((x['override'] for x in dispatch.get('started', []) if 'override' in x), None)
                else:
                    dispatch = tick(config_path, state, runtime=runtime)
                after = {x['id']: x for x in queue.list()}
                actual = dict(dispatch=dispatch, **({'override': forced} if forced else {}), tickets={key: dict(before=before.get(key, {}).get('state'), after=value['state'])
                                                         for key, value in after.items()},
                              observed_count=len(after), completed=[key for key, value in after.items()
                                if value['state']=='completed' and before.get(key, {}).get('state')!='completed'])
                action.update(status='completed', reason='synchronization processed' if action['kind']=='sync' else 'selected dispatch processed', result=actual)
                if dispatch.get('enabled') is not True:
                    action.update(status='blocked', reason='operations disabled; no dispatch or reconciliation processed')
                if action['kind'] == 'send':
                    current = after[request['ticket']]
                    if current['state'] == 'uncertain':
                        action.update(status='uncertain', reason=current.get('reason') or 'delivery unknown; never resend')
                    elif not dispatch.get('started'):
                        action.update(status='blocked', reason=current.get('reason') or 'selected executor no longer available; refresh candidates')
            except Exception as error:
                action.update(status='blocked', reason=str(error))
            except BaseException:
                # A stopped consumer is not proof that native delivery failed.
                # Preserve the action and original attempt; no claim is retried.
                with queue.transaction():
                    action.update(status='uncertain', reason='consumer stopped; inspect original attempt, never resend',
                                  finished_at=time.time())
                    _save(queue.db, action)
                raise
            with queue.transaction():
                action['finished_at'] = time.time()
                _save(queue.db, action)
            results.append(action)
        return results
    finally:
        queue.close()
