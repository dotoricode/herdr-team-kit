"""Official ticket actions; UI clients call these instead of editing the ledger."""
import json
import os
from pathlib import Path

from .config import load
from .operations import NativeRuntime, tick, view
from .tickets import TicketStore


def parser(commands):
    item = commands.add_parser('queue')
    item.add_argument('action', choices=('create', 'approve', 'show', 'status', 'submit', 'receipt',
                                        'wait-children', 'dependencies', 'tick', 'intake', 'classify', 'approval-template', 'organize', 'dismiss', 'candidates', 'send', 'sync', 'action-status'))
    item.add_argument('ticket', nargs='?')
    item.add_argument('--ticket-file')
    item.add_argument('--key')
    item.add_argument('--approval-file')
    item.add_argument('--user-approved', action='store_true')
    item.add_argument('--attempt')
    item.add_argument('--owner')
    item.add_argument('--result-file')
    item.add_argument('--depends-on', action='append', default=[])
    item.add_argument('--release-writes', action='store_true')
    item.add_argument('--board', action='store_true')
    item.add_argument('--board-action', choices=('waiting','progress','done','archive','restore'))
    item.add_argument('--client-request-id')
    item.add_argument('--expected-revision')
    item.add_argument('--reason')
    item.add_argument('--expected-digest')
    item.add_argument('--candidate-id')
    item.add_argument('--expected-ticket')


def handle(args):
    if args.action in ('candidates', 'send', 'sync', 'action-status'):
        from . import queue_actions
        if args.action == 'candidates':
            return queue_actions.candidates(args.config, args.state, args.ticket)
        if args.action == 'action-status':
            return queue_actions.status(args.state, args.client_request_id)
        return queue_actions.enqueue(args.config, args.state, args.action, args.client_request_id,
            ticket=args.ticket, candidate_id=args.candidate_id, expected_ticket=args.expected_ticket)
    if args.action == 'status':
        if args.board:
            from .queue_view import board
            return board(args.config, args.state)
        return view(args.config, args.state)
    if args.action == 'organize':
        from .queue_view import organize
        return organize(args.config, args.state, args.ticket, args.board_action,
                        args.client_request_id, args.expected_revision)
    if args.action == 'tick':
        return tick(args.config, args.state)
    if args.action == 'classify':
        from .intake_dispatch import submit
        if not args.ticket or not args.result_file or Path(args.result_file).stat().st_size > 256*1024:
            raise ValueError('instruction identity and bounded classification JSON file required')
        return submit(load(args.config),args.state,args.ticket,args.attempt,args.owner,
                      os.environ.get('HC_TERMINAL_ID'),json.loads(Path(args.result_file).read_text()))
    queue = TicketStore(args.state)
    try:
        if args.action == 'dismiss':
            from .intake import dismiss
            with queue.transaction():
                return dismiss(queue.db, args.ticket, args.reason, args.expected_digest)
        if args.action == 'intake':
            from .intake import register
            if not args.ticket_file or not args.key:
                raise ValueError('original instruction file and stable source key required')
            packet = Path(args.ticket_file)
            if packet.stat().st_size > 1024*1024:
                raise ValueError('instruction file exceeds bound')
            result = register(queue.db, packet.read_text(), {'key':args.key})
            queue.notify()
            return result
        if args.action == 'create':
            if not args.ticket_file or not args.key:
                raise ValueError('ticket-file and idempotency key required')
            return queue.enqueue(json.loads(Path(args.ticket_file).read_text()), args.key,
                                 user_approved=args.user_approved)
        if not args.ticket:
            raise ValueError('ticket identity required')
        if args.action == 'show':
            return queue.get(args.ticket)
        if args.action == 'approval-template':
            return queue.approval_template(args.ticket)
        if args.action == 'approve':
            if not args.user_approved or not args.approval_file:
                raise ValueError('explicit user-approved action and exact scope approval file required')
            return queue.approve(args.ticket, args.approval_file)
        if args.action == 'dependencies':
            queue.set_dependencies(args.ticket, args.depends_on)
            return queue.get(args.ticket)
        attempt = queue.attempt(args.attempt)
        executor = attempt['executor']
        if os.environ.get('HC_TERMINAL_ID') != executor['terminal'] or args.owner != executor['owner']:
            raise ValueError('only the actual current execution terminal may report this attempt')
        runtime = NativeRuntime(load(args.config), args.state)
        if args.action == 'receipt':
            from .operations import enabled, work_gate
            config_bytes=Path(args.config).read_bytes()
            current_config=load(args.config)
            if not enabled(current_config):
                raise ValueError('new work is disabled by current operations policy')
            from . import docsstaff
            docsstaff.check(current_config, queue.db, queue.get(args.ticket), executor)
            native = runtime.native(executor, require_idle=False)
            work_gate(current_config,args.state,queue.get(args.ticket))
            if Path(args.config).read_bytes()!=config_bytes:
                raise ValueError('configuration changed before actor receipt')
            return queue.receipt(args.ticket, args.attempt, args.owner, executor['terminal'], native)
        if args.action == 'submit':
            runtime.native(executor, require_idle=False)
            if not args.result_file or Path(args.result_file).stat().st_size > 256 * 1024:
                raise ValueError('bounded JSON result file required')
            return queue.submit(args.ticket, args.attempt, args.owner, json.loads(Path(args.result_file).read_text()))
        if args.action == 'wait-children':
            runtime.native(executor, require_idle=False)
            return queue.request_children_wait(args.ticket, args.attempt, args.owner, args.release_writes)
        raise ValueError('unsupported queue action')
    finally:
        queue.close()
