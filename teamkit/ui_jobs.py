"""Fenced ticket adapter for the existing, independently locked UI lab.

This adapter never claims a UI job, operates a screen, retries a submission, or
starts a model. Non-adapter UI work remains outside this ledger's control.
"""
import json
import os
from pathlib import Path
import subprocess
import time

from . import admission, providers
from .config import binding, cached_quota, load, profile
from .core_runtime import CoreRuntime, _call, _model_effort, require_managed
from .policy import eligible
from .pool_control import _file_proof
from .prompt_state import empty_input
from .resources import ResourceLedger
from .runtime import RuntimeError as NativeError
from .store import Store
from .tickets import TicketStore, digest, file_digest, strings, within, execution_resources


BOUNDARY = ('Only linked UI-lab requests are managed; other UI work and physical screen users remain outside control. '
            'The final guard is not atomic with the official subprocess or uncooperative external file writes; '
            'the worker must pass the official ticket receipt before any UI action.')
ERRORS = (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, NativeError)
SCREEN = dict(kind='named', name='screen:test-macbook', mode='write')


def _lab(settings, action, ident=None, payload=None):
    argv = settings.get('command')
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(x, str) or not x for x in argv)):
        raise ValueError('UI lab requires an explicit official CLI argv')
    completed = subprocess.run([*argv, action, *([ident] if ident else [])],
        input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        capture_output=True, text=True, timeout=30)
    if completed.returncode:
        raise ValueError('UI lab ' + action + ' outcome unavailable; inspect original request before retry')
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or result.get('error'):
        raise ValueError('UI lab returned an invalid response')
    return result


def _registry(config, settings, status, executor=None):
    worker = settings.get('worker')
    if not isinstance(worker, dict):
        raise ValueError('UI lab worker binding is missing; no UI request was sent')
    executor = executor or worker
    registry = status.get('registry')
    if (not isinstance(registry, dict) or registry.get('ui_host') != 'test-macbook'
            or registry.get('provider') != 'codex' or registry.get('account') != 'personal'
            or registry.get('terminal') != executor.get('terminal', (executor.get('runtime') or {}).get('terminal'))
            or executor.get('provider') != 'codex' or executor.get('account') != 'personal'):
        raise ValueError('UI lab must retain its registered personal Codex worker on test-macbook')
    pr = profile(config, 'codex', 'personal')
    if (Path(registry.get('profile_home', '')).resolve() != Path(pr['home']).resolve()
            or executor.get('profile_binding') != binding(pr)):
        raise ValueError('UI lab personal profile binding changed or is unverified')
    return pr


def _native(executor, idle=True):
    actor = dict(id=executor['task'], owner=executor['owner'], provider='codex', runtime=executor['runtime'])
    value = CoreRuntime(actor)._native(executor['runtime']['name'], ready=idle)
    if (value.get('foreground') or {}).get('pid') != executor['pid']:
        raise ValueError('UI worker native PID changed')
    if _model_effort('codex', value['foreground'].get('argv', [])) != (executor['model'], executor['effort']):
        raise ValueError('UI worker native model or effort differs from its pinned policy')
    if idle:
        screen = _call('read', '--json', executor['terminal'])
        if screen.get('complete') is not True or not empty_input(screen.get('text', ''), executor.get('empty_prompt')):
            raise ValueError('UI worker has a draft, dialog or incomplete input observation')
    return dict(terminal=executor['terminal'], pid=executor['pid'], owner=executor['owner'],
                status=(value.get('status') or {}).get('state'))


def _available(status):
    jobs = status.get('jobs')
    if not isinstance(jobs, list) or any(not isinstance(x, dict) for x in jobs):
        raise ValueError('UI lab queue observation is unavailable')
    if any(x.get('status') in ('queued', 'running') for x in jobs):
        raise ValueError('UI lab already has queued/running work; preserve its screen ownership')


def _request(ticket, attempt, requester):
    ui = ticket.get('ui')
    if ticket['kind'] != 'ui' or ticket.get('review_required'):
        raise ValueError('UI adapter requires a UI ticket with a separate explicit review workflow when needed')
    if not isinstance(ui, dict) or not isinstance(ui.get('target'), dict) or not ui['target'].get('version'):
        raise ValueError('UI ticket requires its approved target and exact version')
    strings(ui.get('steps'), 'UI steps')
    if not isinstance(ui.get('scope'), str) or not ui['scope'].strip():
        raise ValueError('UI ticket requires an explicit UI approval scope')
    if SCREEN not in ticket['resources']:
        raise ValueError('UI ticket requires the approved screen:test-macbook write lease')
    if 'codex/personal' not in ticket['profiles']:
        raise ValueError('UI ticket does not authorize personal Codex')
    if ticket.get('execution_constraint') not in (None, {'provider': 'codex', 'account': 'personal'}):
        raise ValueError('UI ticket execution constraint conflicts with the fixed UI lab worker')
    decision = ticket.get('user_decision')
    if decision and any(decision.get(k) != attempt['executor'][k] for k in ('provider', 'model', 'effort')):
        raise ValueError('UI ticket explicit model decision differs from the fixed UI lab worker')
    return dict(id='UI-' + digest({'ticket': ticket['id'], 'attempt': attempt['id']})[:40],
        title=ticket['goal'], requester=requester, host='test-macbook', goal=ticket['goal'],
        target=ui['target'], steps=ui['steps'], expected=ticket['completion'], scope=ui['scope'],
        result_contract=dict(ticket=ticket['id'], attempt=attempt['id'], check_keys=ticket['checks'],
            target_version=ui['target']['version'], writer_stopped=True,
            required=['target_version', 'request_digest', 'evidence_sha256', 'writer_stopped'],
            digest_algorithm='SHA-256 of json.dumps(request, sort_keys=True, ensure_ascii=False).encode()',
            explanation='Result must identify this exact request digest, target version and checks; evidence_sha256 maps each evidence path to its SHA-256. Claim only through the official UI lab.'))


def _remember(queue, ticket_id, attempt_id, **detail):
    with queue.transaction():
        ticket = queue.get(ticket_id)
        attempt = queue.attempt(attempt_id)
        if ticket['attempt'] != attempt_id:
            raise ValueError('UI result belongs to an obsolete ticket attempt')
        attempt.update(detail)
        queue._save_attempt(attempt)
        queue._save(ticket, 'ui_lab_observed', {'attempt': attempt_id})


def _before_submit(config_path, config_bytes, config, settings, queue, ticket, attempt, request):
    # Native/input is the last external observation; all following checks are
    # local protocol state and grants, directly before the official submit.
    status = _lab(settings, 'status')
    _registry(config, settings, status, attempt['executor'])
    _available(status)
    _native(attempt['executor'])
    if Path(config_path).read_bytes() != config_bytes:
        raise ValueError('UI execution config changed after reservation; request not submitted')
    from .operations import work_gate
    from .workers import _ticket_signature
    current = queue.get(ticket['id'])
    current_attempt = queue.attempt(attempt['id'])
    if (current['attempt'] != attempt['id'] or current['state'] != 'dispatching'
            or current_attempt['phase'] != 'reserved'
            or current_attempt['executor'] != attempt['executor']
            or _ticket_signature(current) != _ticket_signature(ticket)
            or current.get('ui') != ticket.get('ui')
            or current.get('dependencies') != ticket.get('dependencies')
            or current_attempt.get('ui_delivery', {}).get('request') != request):
        raise ValueError('UI ticket/attempt/executor or failure policy changed after reservation')
    if queue.gate(ticket['id']):
        raise ValueError('UI approval/dependency changed after reservation; request not submitted')
    work_gate(config, queue.path, current)
    pr = profile(config, 'codex', 'personal')
    if current_attempt['executor']['profile_binding'] != binding(pr):
        raise ValueError('UI fixed profile changed after reservation')
    store = Store(queue.path)
    try:
        quota = cached_quota(config, store, 'codex', 'personal')
    finally:
        store.close()
    if not eligible(quota, time.time(), config['quota_ttl_seconds']):
        raise ValueError('UI quota changed after reservation; request not submitted')
    reservation = next((r for r in ResourceLedger._rows(queue.db) if r['attempt'] == attempt['id']), {})
    if (not reservation.get('execution_reserved') or not reservation.get('writes_held')
            or reservation.get('resources')!=execution_resources(current)):
        raise ValueError('UI execution/screen reservation changed before submission')


def _manifest(queue, ticket, attempt, job):
    folder = queue.path.resolve().parent / 'operation-results' / ticket['id']
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        raise ValueError('UI output directory changed')
    path = folder / ('ui-' + attempt['id'] + '.json')
    content = json.dumps(job, sort_keys=True, ensure_ascii=False).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError('UI result snapshot changed; preserve the original result')
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return str(path)


def _passed(settings, ticket, attempt, job):
    result = job.get('result') or {}
    request = attempt['ui_delivery']['request']
    if (result.get('status') != 'passed' or result.get('host') != 'test-macbook'
            or not isinstance(result.get('tool'), str) or not result['tool']
            or result.get('target_version') != request['target']['version']
            or result.get('request_digest') != digest(request)
            or result.get('writer_stopped') is not True):
        raise ValueError('UI pass lacks exact target/version, request, actual host/tool or stopped-writer evidence')
    checks = result.get('checks')
    if (not isinstance(checks, list) or any(not isinstance(x, dict) for x in checks)
            or len(checks) != len(ticket['checks'])
            or {x.get('name') for x in checks} != set(ticket['checks'])
            or any(x.get('status') != 'passed' for x in checks)):
        raise ValueError('UI result does not pass every exact approved check')
    evidence, hashes = result.get('evidence'), result.get('evidence_sha256')
    if (not isinstance(evidence, list) or not 0 < len(evidence) <= 32
            or any(not isinstance(x, str) for x in evidence) or len(set(evidence)) != len(evidence)
            or not isinstance(hashes, dict) or set(hashes) != set(evidence)):
        raise ValueError('UI result requires distinct evidence paths and their exact hashes')
    roots = settings.get('evidence_roots', [])
    if (not isinstance(roots, list) or not roots
            or any(not isinstance(x, str) or not Path(x).is_absolute() for x in roots)):
        raise ValueError('UI lab evidence roots must be explicitly configured')
    for path in evidence:
        if not within(path, roots) or _file_proof(path) != hashes[path]:
            raise ValueError('UI evidence is outside the output boundary or changed after capture')
    return {name: True for name in ticket['checks']}


def _reconcile(config, settings, queue, ticket):
    attempt = queue.attempt(ticket['attempt'])
    delivery = attempt.get('ui_delivery')
    if not delivery:
        raise ValueError('UI reservation has no delivery intent; inspect before any external submission')
    status = _lab(settings, 'status')
    _registry(config, settings, status, attempt['executor'])
    job = _lab(settings, 'result', delivery['request']['id'])
    if job.get('id') != delivery['request']['id'] or job.get('request') != delivery['request']:
        raise ValueError('UI request identity/content differs from the persisted delivery intent')
    phase = job.get('status')
    if phase == 'queued':
        queue.block(ticket['id'], 'UI 검증실 접수됨 · 실제 claim과 화면 실행 확인 대기')
        return
    if phase not in ('running', 'passed', 'failed', 'blocked') or job.get('owner') != attempt['executor']['terminal'] or not job.get('started_at'):
        raise ValueError('UI job has no matching actual worker claim')
    native = _native(attempt['executor'], idle=False)
    if ticket['state'] in ('dispatching', 'received', 'uncertain', 'running'):
        queue.receipt(ticket['id'], attempt['id'], attempt['executor']['owner'], attempt['executor']['terminal'], native)
    if phase == 'running':
        queue.block(ticket['id'], 'UI 검증실 claim 확인됨 · 결과와 실제 쓰기 중단 대기')
        return
    if not job.get('finished_at') or (job.get('result') or {}).get('status') != phase:
        raise ValueError('UI job final result and lifecycle disagree')
    if phase in ('failed', 'blocked'):
        result = job['result']
        if result.get('writer_stopped') is True:
            _native(attempt['executor'])
            ResourceLedger(queue.db).release(attempt['id'], True, True, True)
        if attempt.get('ui_result') != job:
            _remember(queue, ticket['id'], attempt['id'], ui_result=job)
        queue.block(ticket['id'], 'UI 검증실 ' + phase + ' · ' + str(result.get('reason') or result.get('summary') or '원 결과 확인 필요'))
        return
    from .operations import work_gate
    work_gate(config, queue.path, ticket)
    if queue.gate(ticket['id']):
        raise ValueError('UI ticket approval/dependency changed before result adoption')
    checks = _passed(settings, ticket, attempt, job)
    stopped = _native(attempt['executor'])
    artifact = _manifest(queue, ticket, attempt, job)
    if 'submission' not in attempt:
        queue.submit(ticket['id'], attempt['id'], attempt['executor']['owner'],
                     dict(artifact=artifact, writer_stopped=True))
    if not queue.attempt(attempt['id']).get('stop_observation'):
        queue.record_stop(ticket['id'], attempt['id'], stopped)
    _passed(settings, ticket, attempt, job)
    queue.finish(ticket['id'], attempt['id'], dict(artifact_digest=file_digest(artifact), checks=checks,
                 verifier='official-ui-lab-result', external_request=delivery['request']['id']))


def tick(config_path, state):
    config_bytes = Path(config_path).read_bytes()
    config = load(config_path)
    settings = config.get('operations', {}).get('ui_lab', {})
    if config.get('operations', {}).get('enabled') is not True:
        return {'enabled': False, 'coverage': BOUNDARY}
    queue = TicketStore(state)
    submitted = []
    try:
        if not isinstance(settings, dict) or settings.get('enabled') is not True:
            for ticket in queue.list():
                if ticket['role'] == 'ui' and ticket['state'] not in ('completed', 'cancelled'):
                    queue.block(ticket['id'], 'UI adapter is disabled or not configured; explicit UI-lab binding is required')
            return {'enabled': False, 'coverage': BOUNDARY}
        for ticket in queue.list():
            if ticket['role'] != 'ui' or ticket['state'] in ('completed', 'cancelled') or not ticket.get('attempt'):
                continue
            try:
                _reconcile(config, settings, queue, ticket)
            except ERRORS as error:
                queue.block(ticket['id'], str(error))
        for ticket in queue.ready():
            if ticket['role'] != 'ui':
                continue
            try:
                if settings.get('blocked_reason'):
                    raise ValueError('UI host preflight blocked: ' + str(settings['blocked_reason']))
                worker = settings.get('worker')
                if not isinstance(worker, dict):
                    raise ValueError('UI lab worker binding is missing; no UI request was sent')
                executor = {**worker, 'terminal': worker['runtime']['terminal'], 'pid': worker['runtime']['native_pid'],
                            'product': ticket['product'], 'roles': ['ui']}
                provisional = {'id': 'preflight', 'executor': executor}
                _request(ticket, provisional, 't0')
                from .operations import policy, work_gate
                work_gate(config, state, ticket)
                status = _lab(settings, 'status')
                pr = _registry(config, settings, status, executor)
                _available(status)
                _native(executor)
                providers.identity('codex', pr)
                store = Store(state)
                try:
                    quota = cached_quota(config, store, 'codex', 'personal')
                finally:
                    store.close()
                if not eligible(quota, time.time(), config['quota_ttl_seconds']):
                    raise ValueError('UI personal Codex quota is unavailable, expired or limited')
                requester = require_managed()
                status = _lab(settings, 'status')
                _registry(config, settings, status, executor)
                _available(status)
                _native(executor)
                admission.configure(queue.db, policy(config))
                attempt = queue.reserve(ticket['id'], executor, policy(config), admission.observe(queue.db))
                request = _request(ticket, attempt, requester)
                from .context_handoff import cli
                import shlex
                receipt = shlex.join(cli(config_path, state, 'queue', 'receipt', ticket['id'],
                                        '--attempt', attempt['id'], '--owner', executor['owner']))
                from .protocol import tool_permission_hint
                request['steps'] = [tool_permission_hint('codex')+'Before any screen/browser/UI action, run this exact official ticket receipt: '
                                    + receipt + '. If receipt fails or approval is unavailable, stop; do not perform any UI steps.',
                                    *request['steps']]
                _remember(queue, ticket['id'], attempt['id'], ui_delivery=dict(phase='intent', request=request,
                          request_digest=digest(request), created_at=time.time()))
                try:
                    _before_submit(config_path, config_bytes, config, settings, queue, ticket, attempt, request)
                    response = _lab(settings, 'submit', payload=request)
                    if response.get('id') != request['id']:
                        raise ValueError('UI submit response differs from the original request')
                    queue.delivery(attempt['id'], True, response)
                    submitted.append(dict(ticket=ticket['id'], attempt=attempt['id'], request=request['id']))
                except ERRORS as error:
                    queue.delivery(attempt['id'], False, {'reason': str(error)})
            except ERRORS as error:
                queue.block(ticket['id'], str(error))
        return {'enabled': True, 'submitted': submitted, 'coverage': BOUNDARY}
    finally:
        queue.close()
