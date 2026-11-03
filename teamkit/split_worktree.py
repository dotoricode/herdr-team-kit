"""Checked Git worktree preparation for an already-approved split intent.

This module does not decide admission or mutate the ticket database. Its caller
must persist and fence the returned intent before calling create().
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess


_TICKET = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_SHA = re.compile(r'[a-f0-9]{40}|[a-f0-9]{64}\Z')


def _git(root, *arguments):
    env = os.environ.copy()
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(name, None)
    try:
        result = subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', *arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                timeout=30, check=True, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('git 저장소 또는 worktree를 확인할 수 없습니다') from error
    return result.stdout.strip()


def _existing_parent(path):
    path = Path(path).resolve()
    while not path.exists():
        if path == path.parent:
            raise ValueError('git 저장소 경로를 찾을 수 없습니다')
        path = path.parent
    return path if path.is_dir() else path.parent


def plan(ticket_id, resources, destination_root, *, base=None, ref=None):
    """Build a read-only immutable proposal; every mapped path belongs to one repo."""
    from .resources import normalize_resources
    if not isinstance(ticket_id, str) or not _TICKET.fullmatch(ticket_id):
        raise ValueError('invalid ticket identity')
    normalized = normalize_resources(resources)
    paths = [r for r in normalized if r['kind'] == 'path']
    if not any(r['mode'] == 'write' for r in paths):
        raise ValueError('worktree split requires a path write')
    source = None
    for resource in paths:
        root = Path(_git(_existing_parent(resource['name']), 'rev-parse', '--show-toplevel')).resolve()
        if source is None:
            source = root
        if root != source:
            raise ValueError('all split paths must be in the same git worktree')
    if base is not None and (not isinstance(base, str) or not _SHA.fullmatch(base)):
        raise ValueError('git base is not a full commit identity')
    if ref is not None and (base is not None or not isinstance(ref, str)
                            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', ref)
                            or '..' in ref or ref.endswith(('.', '/'))):
        raise ValueError('invalid explicit git base reference')
    expected_base = base
    base = _git(source, 'rev-parse', '--verify', '--end-of-options', (base or ref or 'HEAD') + '^{commit}').lower()
    if not _SHA.fullmatch(base):
        raise ValueError('git HEAD is not a full commit identity')
    if expected_base is not None and base != expected_base:
        raise ValueError('git base changed')
    # Use a hash rather than an untrusted path component or a globally shared ticket ID.
    token = hashlib.sha256((str(source)+'\0'+ticket_id+'\0'+base).encode()).hexdigest()[:24]
    branch = 'teamkit/split-' + token
    target = (Path(destination_root).expanduser().resolve() / ('split-' + token)).resolve()
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('split worktree must be disjoint from the original worktree')
    # Preserve declaration order, including named locks, rather than expanding permissions.
    mapped = []
    for resource in resources:
        value = dict(resource)
        if value['kind'] == 'path':
            path = Path(value['name']).resolve()
            if not path.is_relative_to(source):
                raise ValueError('path outside git worktree')
            value['name'] = str(target / path.relative_to(source))
        mapped.append(value)
    return dict(ticket=ticket_id, source_root=str(source), base=base, worktree=str(target),
                branch=branch, resources=mapped)


def _check_existing(intent):
    source, target = Path(intent['source_root']), Path(intent['worktree'])
    if not target.is_dir() or target.is_symlink():
        raise ValueError('split destination already exists but is not a trusted worktree')
    registered = _git(source, 'worktree', 'list', '--porcelain').splitlines()
    if 'worktree ' + str(target) not in registered:
        raise ValueError('split destination is not registered to the original repository')
    if Path(_git(target, 'rev-parse', '--show-toplevel')).resolve() != target:
        raise ValueError('split worktree root differs from intent')
    if _git(target, 'symbolic-ref', '--quiet', '--short', 'HEAD') != intent['branch']:
        raise ValueError('split worktree branch differs from intent')
    _git(target, 'merge-base', '--is-ancestor', intent['base'], 'HEAD')


def create(intent):
    """Create or verify the intended worktree; never reset or delete existing data."""
    # Reconstruct and compare all derived fields before filesystem or Git changes.
    source = Path(intent['source_root']).resolve()
    paths = []
    target = Path(intent['worktree']).resolve()
    for resource in intent['resources']:
        value = dict(resource)
        if value['kind'] == 'path':
            path = Path(value['name']).resolve()
            if not path.is_relative_to(target):
                raise ValueError('mapped path escaped the intended worktree')
            value['name'] = str(source / path.relative_to(target))
        paths.append(value)
    expected = plan(intent['ticket'], paths, target.parent, base=intent['base'])
    if expected != intent:
        raise ValueError('worktree intent no longer matches source HEAD or derived paths')
    if target.exists() or target.is_symlink():
        _check_existing(intent)
        return intent
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(source, 'worktree', 'add', '-b', intent['branch'], str(target), intent['base'])
    except ValueError:
        # A concurrent creator may have finished. Never repair/remove anything here.
        _check_existing(intent)
    _check_existing(intent)
    return intent


def mapped_scope(ticket, intent):
    """Translate only the source checkout's approved paths; retain scope actions and data."""
    source, target = Path(intent['source_root']), Path(intent['worktree'])
    paths = []
    for raw in ticket['scope']['paths']:
        approved = Path(raw).resolve()
        if approved.is_relative_to(source):
            paths.append(str(target / approved.relative_to(source)))
        elif source.is_relative_to(approved):
            paths.append(str(target))
    if not paths:
        raise ValueError('split worktree is outside the approved source paths')
    return {**ticket['scope'], 'paths': list(dict.fromkeys(paths))}
