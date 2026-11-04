"""Operator adoption of external review evidence for an unlaunched required child.

No actor receipt or submission is manufactured. Only the required-child edge is
satisfied, and only while the exact operator-approved evidence remains valid.
"""
import json
from pathlib import Path
from .tickets import digest, file_digest

CONTRACT = ('goal', 'scope', 'resources', 'checks', 'verification_commands', 'approval',
            'execution_constraint', 'exclude_owners', 'parent_id', 'parent_attempt', 'relation')
PREFLIGHT_ERROR = {'error': 'ValueError', 'reason': 'product execution requires linked approved work'}


def _packet(path):
    if Path(path).stat().st_size > 256*1024:
        raise ValueError('bounded replacement evidence required')
    return json.loads(Path(path).read_text())


def _validate(queue, child, attempt, packet):
    parent = queue.get(child.get('parent_id'))
    origin = queue.attempt(parent['attempt'])
    submitted = origin.get('submission') or {}
    reviewer = packet.get('reviewer') or {}
    constraint = child.get('execution_constraint') or {}
    if (child['role'] != 'reviewer' or child['kind'] != 'review' or child.get('relation') != 'required'
            or child['scope']['actions'] != ['read'] or any(r['mode'] != 'read' for r in child['resources'])
            or parent['kind'] != 'review' or parent.get('review_required')
            or child.get('parent_attempt') != origin['id']
            or parent['state'] not in ('awaiting_verification', 'awaiting_review', 'completed')
            or submitted.get('writer_stopped') is not True or not origin.get('stop_observation')
            or not submitted.get('source') or queue.gate(child['id']) or queue.gate(parent['id'])):
        raise ValueError('replacement requires approved read-only required review and stopped parent submission')
    if (packet.get('child') != child['id'] or packet.get('attempt') != attempt['id']
            or packet.get('parent') != parent['id'] or packet.get('parent_attempt') != origin['id']
            or packet.get('contract_digest') != digest({k: child.get(k) for k in CONTRACT})
            or packet.get('parent_artifact_digest') != submitted.get('artifact_digest')
            or file_digest(submitted['artifact']) != submitted['artifact_digest']
            or packet.get('source') != submitted['source']
            or packet.get('artifact_digest') != file_digest(packet['artifact'])
            or not Path(packet['artifact']).stat().st_size
            or packet.get('approval_digest') != file_digest(packet['approval_reference'])):
        raise ValueError('replacement identity, approval, exact source or artifact changed')
    if (not isinstance(reviewer.get('identity'), str) or not reviewer['identity'].strip()
            or reviewer['identity'] == origin['executor']['owner']
            or reviewer.get('not_parent_author') is not True
            or reviewer.get('provider') not in ('codex', 'claude')
            or reviewer.get('account') not in ('team', 'personal')
            or reviewer['provider']+'/'+reviewer['account'] not in child['profiles']
            or any(reviewer.get(k) != v for k, v in constraint.items() if k in ('provider','account'))
            or origin['executor']['owner'] not in child.get('exclude_owners', [])
            or not isinstance(packet.get('independence_evidence'), str) or not packet['independence_evidence'].strip()
            or packet.get('verdict') != 'pass' or not isinstance(packet.get('findings'), list)
            or any(not isinstance(f, dict) or f.get('blocking') is not False for f in packet['findings'])
            or not isinstance(packet.get('reason'), str) or not packet['reason'].strip()):
        raise ValueError('explicit operator-attested independent reviewer provenance required')
    return parent, origin


def satisfied(queue, child):
    """Fail closed if any source, approval, result or parent linkage changes."""
    proof = child.get('required_review_replacement')
    if child['state'] != 'cancelled' or not isinstance(proof, dict):
        return False
    try:
        if file_digest(proof['evidence_file']) != proof['evidence_digest']:
            return False
        packet = _packet(proof['evidence_file'])
        attempt = queue.attempt(child['attempt'])
        _validate(queue, child, attempt, packet)
        original_attempt = {k:v for k,v in attempt.items() if k != 'replacement'}
        original_attempt['phase'] = 'uncertain'
        return (digest(original_attempt) == proof.get('original_attempt_digest')
                and attempt['phase'] == 'preflight_replaced'
                and attempt.get('replacement') == proof
                and proof.get('authority') == 'operator'
                and proof.get('checks') == {name: True for name in child['checks']})
    except (ValueError, KeyError, TypeError, OSError):
        return False


def replace(queue, ident, attempt_id, evidence_file, *, operator_approved=False):
    if operator_approved is not True:
        raise ValueError('explicit operator approval of independent evidence required')
    evidence_file = str(Path(evidence_file).absolute())
    evidence_digest = file_digest(evidence_file)
    packet = _packet(evidence_file)
    from .resources import ResourceLedger
    from .operations import verify
    with queue.transaction():
        child = queue.get(ident)
        attempt = queue.attempt(attempt_id)
        if child.get('required_review_replacement'):
            if satisfied(queue, child) and child['required_review_replacement']['evidence_digest'] == evidence_digest:
                return child
            raise ValueError('existing replacement evidence differs')
        ex = attempt['executor']
        if (child['state'] != 'uncertain' or child['attempt'] != attempt_id
                or attempt['ticket'] != ident or attempt['phase'] != 'uncertain'
                or attempt.get('delivery') != PREFLIGHT_ERROR
                or any(attempt.get(k) for k in ('launch_intent','launch_phase','actor_receipt','submission','stop_observation'))
                or ex.get('new') is not True or ex.get('pid') is not None or ex.get('terminal') is not None
                or ex.get('runtime')):
            raise ValueError('only the exact unlaunched preflight failure can be replaced')
        staged = None
        # A staged task is created before check_task but must still be unclaimed.
        if queue.db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
            row = queue.db.execute('SELECT body FROM tasks WHERE id=?',(ex.get('task'),)).fetchone()
            if row:
                staged = json.loads(row[0])
                if (staged.get('state') != 'queued' or staged.get('owner') or staged.get('runtime')
                        or staged.get('team') != child['product'] or staged.get('role') != ex.get('policy_role')
                        or staged.get('root') != ex.get('actor_root') or staged.get('base') != ex.get('base')
                        or staged.get('goal') != child['goal']):
                    raise ValueError('staged native task has execution evidence')
        parent, origin = _validate(queue, child, attempt, packet)
        # Run the original approved task-specific checks over the adopted artifact.
        verification = verify(child, {'submission': {'artifact': packet['artifact'],
            'artifact_digest': packet['artifact_digest'], 'source': packet['source']}})
        if not all(verification['checks'].values()):
            raise ValueError('replacement review failed original checks')
        if file_digest(evidence_file) != evidence_digest:
            raise ValueError('replacement evidence changed during validation')
        _validate(queue, child, attempt, packet)
        proof = dict(authority='operator', evidence_file=evidence_file,evidence_digest=evidence_digest,
                     checks=verification['checks'], verification=verification, original_attempt_digest=digest(attempt), at=queue.clock(),
                     disclosure='External review adopted by operator; native child never executed; no receipt or submission')
        ResourceLedger(queue.db).release_in_transaction(queue.db, attempt_id, True,
                                                         release_writes=True, writer_stopped=True)
        attempt.update(phase='preflight_replaced', replacement=proof)
        queue._save_attempt(attempt)
        if staged is not None:
            from .store import Store
            Store.cancel_unclaimed_in_transaction(queue.db, staged, proof)
        child.update(state='cancelled',required_review_replacement=proof,
                     reason='Unlaunched review replaced with operator-verified external review evidence',
                     next_action='Preserve original attempt and replacement audit; parent verifies its own result')
        queue._save(child,'required_review_replaced',dict(attempt=attempt_id,**proof))
    queue.notify()
    return child
