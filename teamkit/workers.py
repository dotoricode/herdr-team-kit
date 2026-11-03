"""Ticket-scoped native workers using existing role, identity and launch rules.

An actor is only an HC identity mirror. The ticket and its fenced attempt remain
the lifecycle authority; this module never sends a model prompt or reserves a
second execution slot.
"""
import hashlib
import json
from pathlib import Path
import re
import time
import tomllib
import uuid

from . import providers
from .config import binding, cached_quota, load, profile
from .core_runtime import CoreRuntime, _call, claude_gui_settings, require_managed
from .policy import account_order, eligible
from .prompt_state import empty_input
from .resources import ResourceLedger
from .routing import execution_candidates, execution_policy
from .runtime import RuntimeError as NativeError, launch_spec
from .store import Store
from .tickets import TicketStore, digest, within, execution_resources


DEFAULT_ROLES = {'leader': 'leader', 'investigator': 'leader',
                 'implementer': 'implementer', 'reviewer': 'reviewer'}
POLICY_FIELDS = ('user_decision', 'failures', 'execution_constraint', 'classification')
PLAN_FIELDS = ('provider', 'account', 'model', 'effort', 'product', 'roles', 'root',
               'actor_root', 'policy_role', 'profile_binding', 'ticket_signature', 'base', 'basis_kind')


def _ticket_signature(ticket):
    fields = ('id', 'origin_id', 'parent_id', 'product', 'role', 'kind', 'goal', 'completion',
              'checks', 'scope', 'approval', 'profiles', 'resources', 'workflow_ref',
              'git_base', *POLICY_FIELDS)
    return digest({key: ticket.get(key) for key in fields})


def _atlassian_registered(provider, pr):
    home = Path(pr['home']).expanduser()
    if provider == 'codex':
        path = home / 'config.toml'
        try:
            entry = tomllib.loads(path.read_text()).get('mcp_servers', {}).get('atlassian')
        except (OSError, ValueError):
            return False
    else:
        path = (Path.home() / '.claude.json') if home == Path.home() / '.claude' else home / '.claude.json'
        try:
            entry = json.loads(path.read_text()).get('mcpServers', {}).get('atlassian')
        except (OSError, ValueError):
            return False
    return isinstance(entry, dict) and entry.get('enabled', True) is not False and bool(entry.get('url') or entry.get('command'))


def preferred(ticket, provider, account):
    """True when the ticket designates this account (account list or execution constraint).

    A designation only orders candidates; it never decides whether one is eligible.
    """
    constraint = ticket.get('execution_constraint')
    if isinstance(constraint, dict) and (constraint.get('provider'), constraint.get('account')) == (provider, account):
        return True
    return provider + '/' + account in (ticket.get('profiles') or [])


def prefer_designated(ticket, items, key=lambda item: item):
    """Stable partition: designated accounts first, the existing order inside each group."""
    return sorted(items, key=lambda item: not preferred(ticket, *key(item)))


def _policy_task(config, ticket):
    product = ticket['product']
    if product not in config['teams']:
        return None
    mapping = config.get('operations', {}).get('worker_roles', {})
    if not isinstance(mapping, dict):
        raise ValueError('worker_roles must map ticket roles to existing product role names')
    role = mapping.get(ticket['role'], DEFAULT_ROLES.get(ticket['role']))
    if role is not None and not isinstance(role, str):
        raise ValueError('worker role mapping must name an existing product role')
    if role not in config['teams'][product]['roles']:
        return None
    task = dict(team=product, role=role, source_role=ticket['role'], classification=ticket.get('classification', {'kind': 'general', 'risks': []}))
    task.update({key: ticket[key] for key in POLICY_FIELDS if key in ticket})
    return task


def _roots(config, ticket, queue=None):
    roots = [Path(path).resolve() for path in ticket['scope']['paths'] if Path(path).is_dir()]
    if not roots:
        raise ValueError('worker requires an existing approved directory')
    root = roots[0]
    if any(r['kind'] == 'path' and not within(r['name'], [str(root)]) for r in ticket['resources']):
        raise ValueError('worker cwd does not cover the approved path resources')
    team_root = Path(config['teams'][ticket['product']]['root']).resolve()
    allowed = [str(team_root), *config.get('shared_roots', [])]
    if not within(str(root), allowed):
        inherited = ticket.get('inherited_worktree')
        if not inherited or queue is None or not ticket.get('parent_id'):
            raise ValueError('worker root is outside configured product/shared roots')
        parent = queue.get(ticket['parent_id'])
        if (parent.get('worktree_split') or parent.get('inherited_worktree')) != inherited or not within(str(root), [inherited['worktree']]):
            raise ValueError('worker root is outside its parent split worktree')
        from .split_worktree import _check_existing
        _check_existing(inherited)
    reference = ticket.get('workflow_ref')
    actor_root = Path(reference['root']).resolve() if reference else root
    return str(root), str(actor_root)


def plans(config, state, ticket, *, reserved_owner=None):
    """Return eligible fresh workers without creating sessions or editing profiles."""
    if config.get('operations', {}).get('enabled') is not True:
        return []
    from .operations import work_gate
    from . import docsstaff
    staff = docsstaff.policy(config, ticket)
    if staff is None and docsstaff.department(config, ticket['product']):
        return []
    work_gate(config, state, ticket)
    from .departments import execution_config
    config = execution_config(config)
    queue = TicketStore(state)
    store = Store(state)
    try:
        if _ticket_signature(queue.get(ticket['id'])) != _ticket_signature(ticket):
            raise ValueError('ticket contract changed before worker planning')
        if queue.gate(ticket['id']):
            return []
        task = _policy_task(config, ticket)
        if task is None:
            return []
        root, actor_root = _roots(config, ticket, queue)
        role = execution_policy(config, task)
        if role['action'] != 'dispatch':
            return []
        candidate_config = config
        if staff:
            candidate_config = docsstaff.routing_config(config, role)
        candidates = execution_candidates(candidate_config, task, role, time.time())
        if candidates is None:
            candidates = [{**role, 'account': account} for account in account_order(config, time.time())]
        from .__main__ import instructions_basis, git
        basis_kind = 'git' if ticket.get('workflow_ref') else 'instructions'
        base = git(actor_root, 'rev-parse', 'HEAD') if basis_kind == 'git' else instructions_basis(config['teams'][ticket['product']])
        result = []
        for candidate in candidates:
            provider, account = candidate['provider'], candidate['account']
            if staff and (provider + '/' + account not in staff['profiles']
                          or not docsstaff.available(config, queue.db, ticket, provider, reserved_owner)):
                continue
            pr = profile(config, provider, account, candidate)
            if (not eligible(cached_quota(config, store, provider, account), time.time(), config['quota_ttl_seconds'])
                    or not _atlassian_registered(provider, pr)):
                continue
            try:
                providers.identity(provider, pr)
            except (ValueError, OSError, RuntimeError, NativeError):
                continue
            if not eligible(cached_quota(config, store, provider, account), time.time(), config['quota_ttl_seconds']):
                continue
            owner = uuid.uuid4().hex
            task_id = 'OP-' + hashlib.sha256(ticket['id'].encode()).hexdigest()[:16] + '-' + owner[:20]
            result.append(dict(provider=provider, account=account, model=candidate['model'],
                               effort=candidate.get('effort', pr.get('effort', 'medium')),
                               product=ticket['product'], roles=[ticket['role']], root=root,
                               actor_root=actor_root, policy_role=task['role'], owner=owner, task=task_id,
                               new=True, pid=None, terminal=None, runtime={}, empty_prompt=None,
                               operation_actor=True, profile_binding=binding(pr),
                               ticket_signature=_ticket_signature(ticket), base=base, basis_kind=basis_kind))
        return prefer_designated(ticket, result, lambda plan: (plan['provider'], plan['account']))
    finally:
        store.close()
        queue.close()


def _attempt(store, ident):
    row = store.db.execute('SELECT body FROM operation_attempts WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('unknown reserved operation attempt')
    return json.loads(row[0])


def _current_ticket(store, ident):
    row = store.db.execute('SELECT body FROM operation_tickets WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('unknown operation ticket')
    return json.loads(row[0])


def _fence(store, ticket, attempt):
    item, current = _current_ticket(store, ticket['id']), _attempt(store, attempt['id'])
    if (item.get('attempt') != attempt['id'] or item['state'] != 'dispatching'
            or current['phase'] != 'reserved' or current['ticket'] != ticket['id']
            or current['executor']['owner'] != attempt['executor']['owner']
            or _ticket_signature(item) != attempt['executor']['ticket_signature']):
        raise ValueError('worker attempt, owner, ticket or approval scope changed')
    lease=next((r for r in ResourceLedger._rows(store.db) if r['attempt']==attempt['id']),{})
    if (not lease.get('execution_reserved') or not lease.get('writes_held')
            or lease.get('resources')!=execution_resources(item, current)):
        raise ValueError('worker resource reservation differs from the host execution contract')
    return item, current


def _save_attempt(store, attempt):
    store.db.execute('UPDATE operation_attempts SET body=? WHERE id=? AND ticket=?',
                     (json.dumps(attempt), attempt['id'], attempt['ticket']))


def _hint(executor, native):
    screen = _call('read', '--json', executor['terminal'])
    if screen.get('complete') is not True:
        raise ValueError('new worker screen is incomplete')
    text = screen.get('text', '')
    if empty_input(text):
        return None
    from .session_pool import _hint_guard
    slot = dict(provider=executor['provider'], phase='preparing')
    prompts = list(re.finditer(r'^[ \t]*[›❯][^\S\r\n]*(.*)$', text, re.M))
    hint = prompts[-1].group(1).strip() if prompts else None
    if (not _hint_guard(slot, native) or not isinstance(hint, str)
            or not re.fullmatch(r'Try "[^"\r\n]{1,200}"', hint) or not empty_input(text, hint)):
        raise ValueError('new worker input is not demonstrably empty')
    actor = dict(id=executor['task'], owner=executor['owner'], provider=executor['provider'], runtime=executor['runtime'])
    after = CoreRuntime(actor)._native(executor['runtime']['name'], ready=True)
    if not _hint_guard(slot, after) or after.get('foreground') != native.get('foreground'):
        raise ValueError('new worker changed while learning its initial hint')
    return hint


def launch(config_path, state, ticket, attempt):
    """Open one blank CLI for an already reserved attempt; never prompt/retry."""
    require_managed()
    config_bytes = Path(config_path).read_bytes()
    config = load(config_path)
    store = Store(state)
    queue = TicketStore(state)
    intent_written = False
    try:
        item, current = _fence(store, ticket, attempt)
        executor = current['executor']
        if current.get('launch_intent'):
            if current.get('launch_phase') == 'ready' and executor.get('pid') and executor.get('terminal'):
                actor = store.get(executor['task'])
                CoreRuntime(actor)._native(executor['runtime']['name'], ready=True)
                return executor
            raise ValueError('worker launch already attempted; preserve its known or uncertain native')
        if executor.get('new') is not True or executor.get('terminal') is not None or executor.get('pid') is not None:
            raise ValueError('worker launch requires an unlaunched reserved worker plan')
        candidates = plans(config, state, item, reserved_owner=executor['owner'])
        if not any(all(candidate.get(key) == executor.get(key) for key in PLAN_FIELDS) for candidate in candidates):
            raise ValueError('worker role, profile, model, quota or approval changed before launch')
        if Path(config_path).read_bytes() != config_bytes:
            raise ValueError('worker execution configuration changed during identity checks')
        reference = item.get('workflow_ref')
        staged = store.create(executor['task'], item['product'], executor['policy_role'], executor['actor_root'],
                              executor['base'], item['goal'], basis_kind=executor['basis_kind'],
                              classification=item.get('classification'),
                              work_id=reference['work_id'] if reference else None,
                              design_digest=reference['digest'] if reference else None,
                              design_revision=reference['revision'] if reference else None)
        with store.transaction():
            item, current = _fence(store, ticket, attempt)
            if current.get('launch_intent') or store.get(executor['task']) != staged or staged['state'] != 'queued':
                raise ValueError('worker actor was already bound or its contract changed')
            if queue.gate(ticket['id']):
                raise ValueError('worker approval/dependencies changed before launch intent')
            from .operations import work_gate
            work_gate(config, state, item)
            from .workflow import check_task
            ticket_scoped_department = (
                item['product'] in config.get('operations', {}).get('departments', {})
                and item['kind'] in ('document', 'investigation', 'ui')
                and not item.get('workflow_ref'))
            if not ticket_scoped_department:
                check_task(config, store, staged)
            reservation = next((r for r in ResourceLedger._rows(store.db) if r['attempt'] == attempt['id']), None)
            if (not reservation or not reservation['execution_reserved'] or not reservation['writes_held']
                    or reservation['ticket'] != item['id']
                    or reservation['resources']!=execution_resources(item, current)
                    or reservation['profile'] != executor['provider'] + '/' + executor['account']
                    or reservation.get('session') is not None):
                raise ValueError('worker has no matching unbound execution reservation')
            name = 'tk-' + executor['owner'][:20]
            staged.update(state='operation_actor', operation_actor=True, operation_ticket=item['id'],
                          operation_attempt=attempt['id'], owner=executor['owner'], provider=executor['provider'],
                          account=executor['account'], model=executor['model'], effort=executor['effort'], attempt_count=1,
                          runtime=dict(name=name, backend='core', resource_attempt=attempt['id'],
                                       profile_binding=executor['profile_binding'], identity_checked=False))
            store._save(staged, 'operation_actor_bound', {'attempt': attempt['id']})
            if current.get('worktree_split'):
                current['executor']['execution_root'] = current['worktree_split']['worktree']
            current['executor']['runtime'] = dict(staged['runtime'])
            current.update(launch_intent={'at': time.time(), 'task': staged['id'], 'owner': executor['owner']},
                           launch_phase='launching', model_startup_started_at=time.time())
            _save_attempt(store, current)
            intent_written = True

        def created(detail):
            detail = {**detail, 'ephemeral': True}
            with store.transaction():
                _, fresh = _fence(store, ticket, attempt)
                native_executor = fresh['executor']
                if (native_executor.get('terminal') is not None
                        and detail.get('terminal') != native_executor['terminal']):
                    raise ValueError('worker launch callback changed native terminal')
                native_executor['runtime'].update(detail)
                native_executor['terminal'] = detail.get('terminal')
                native_executor['pid'] = detail.get('native_pid', native_executor.get('pid'))
                actor = store.get(native_executor['task'])
                actor['runtime'].update(detail)
                if native_executor.get('pid'):
                    ResourceLedger(store.db).bind(attempt['id'], dict(terminal=native_executor['terminal'],
                        pid=native_executor['pid'], owner=native_executor['owner']))
                store._save(actor, 'operation_native_created', {'attempt': attempt['id']})
                _save_attempt(store, fresh)

        pr = profile(config, executor['provider'], executor['account'], executor)
        spec = launch_spec(executor['provider'], pr, None)
        if executor['provider'] == 'claude':
            spec = claude_gui_settings(spec, str(config_path), str(state), executor['account'],
                                        config.get('claude_display_commands', {}).get(executor['account'], ''))
        spec['args'] += ['--add-dir', str(Path(state).resolve().parent)]
        spec['env'].update(TEAMKIT_TASK=executor['task'], TEAMKIT_OWNER=executor['owner'],
                           TEAMKIT_CONFIG=str(Path(config_path).resolve()), TEAMKIT_STATE=str(Path(state).resolve()))
        actor = store.get(executor['task'])
        execution_root = current['executor'].get('execution_root', executor['root'])
        CoreRuntime().launch({**actor, 'root': execution_root}, spec, actor['runtime']['name'], created, keep=False)
        fresh = _attempt(store, attempt['id'])
        executor = fresh['executor']
        actor = store.get(executor['task'])
        native = CoreRuntime(actor)._native(executor['runtime']['name'], ready=True)
        if current.get('worktree_split'):
            observed_cwd = (native.get('foreground') or {}).get('cwd') or native.get('cwd')
            if not observed_cwd or Path(observed_cwd).resolve() != Path(execution_root).resolve():
                raise ValueError('new worker did not start in the split worktree')
        hint = _hint(executor, native)
        with store.transaction():
            _, fresh = _fence(store, ticket, attempt)
            fresh['executor']['empty_prompt'] = hint
            fresh['executor']['new'] = False
            fresh.update(launch_phase='ready', model_startup_ready_at=time.time())
            fresh['model_startup_seconds'] = fresh['model_startup_ready_at'] - fresh['model_startup_started_at']
            _save_attempt(store, fresh)
        return fresh['executor']
    except BaseException as error:
        if intent_written:
            with store.transaction():
                current = _attempt(store, attempt['id'])
                if current['phase'] == 'reserved':
                    current.update(launch_phase='uncertain', launch_error=type(error).__name__)
                    _save_attempt(store, current)
        raise
    finally:
        queue.close()
        store.close()
