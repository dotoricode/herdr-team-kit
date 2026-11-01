"""Private, versioned design approval and restart records for product work."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import time

from .checkpoint import fingerprint, revision_guard
from .store import ACTIVE


def git(root, *args):
    return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True).stdout


def outside_git(path):
    """Inspect the nearest existing parent before creating private files."""
    current=Path(path)
    while not current.exists():
        current=current.parent
    for args in (('rev-parse','--show-toplevel'),('rev-parse','--absolute-git-dir')):
        result=subprocess.run(['git','-C',str(current),*args],capture_output=True)
        if result.returncode == 0:
            raise ValueError('private plans must remain outside every Git repository')
        if b'not a git repository' not in result.stderr:
            raise ValueError('cannot establish private storage Git boundary')


def private_root(config, create=False):
    raw=Path(config.get('private_plan_root',Path.home()/'.local/state/herdr-team-kit/private-plans')).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError('private plan root must be an absolute non-symlink directory')
    root=raw.resolve()
    outside_git(root)
    from .__main__ import paths_overlap
    for accounts in config['profiles'].values():
        for pr in accounts.values():
            if paths_overlap(root,Path(pr['home']).resolve()):
                raise ValueError('private plans overlap an authentication home')
    if create:
        if root.exists() and root.stat().st_mode & 0o077:
            raise ValueError('existing private root is shared; select a private directory without changing unrelated permissions')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root


def strings(value):
    return isinstance(value,list) and bool(value) and all(isinstance(x,str) and x.strip() for x in value)


def validate_contract(contract):
    if not isinstance(contract,dict) or set(contract)!={'title','requirements','allowed_paths','steps'} or not isinstance(contract['title'],str) or not contract['title'].strip() or not strings(contract['requirements']) or not strings(contract['allowed_paths']):
        raise ValueError('contract requires title, requirements, allowed_paths and weighted steps')
    for name in contract['allowed_paths']:
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '.git' in path.parts or name in ('.','./') or '\\' in name:
            raise ValueError('scope requires bounded repository-relative paths')
    steps=contract['steps']
    if not isinstance(steps,list) or not steps:
        raise ValueError('contract requires steps')
    seen=set()
    for step in steps:
        if not isinstance(step,dict) or set(step)!={'id','title','weight'} or not isinstance(step['id'],str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',step['id']) or step['id'] in seen or not isinstance(step['title'],str) or not step['title'].strip() or type(step['weight']) is not int or not 1<=step['weight']<=100:
            raise ValueError('invalid or duplicate milestone')
        seen.add(step['id'])
    if sum(s['weight'] for s in steps)!=100:
        raise ValueError('approved milestone weights must sum to 100')


def setup(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS work_items (id TEXT PRIMARY KEY, body TEXT NOT NULL)')


def get(store, ident):
    setup(store)
    row=store.db.execute('SELECT body FROM work_items WHERE id=?',(ident,)).fetchone()
    if not row:
        raise ValueError('unknown product work')
    return json.loads(row[0])


def save(store, work, event):
    work['updated_at']=time.time()
    store.db.execute('INSERT OR REPLACE INTO work_items VALUES (?,?)',(work['id'],json.dumps(work)))
    store.db.execute('INSERT INTO events(task,event,at,body) VALUES (?,?,?,?)',
                     ('work:'+work['id'],event,work['updated_at'],json.dumps({'revision':work['revision']})))
    return work


def document(config, work):
    root=private_root(config)
    if work.get('private_root')!=str(root):
        raise ValueError('private design storage changed; restore the original path before execution')
    path=root/work['id']/('r'+str(work['revision']))
    if path.is_symlink() or path.resolve()!=path or (path/'plan.md').is_symlink() or (path/'contract.json').is_symlink():
        raise ValueError('private revision path changed')
    outside_git(path)
    plan=(path/'plan.md').read_bytes()
    contract=json.loads((path/'contract.json').read_text())
    validate_contract(contract)
    if not plan.strip():
        raise ValueError('empty design')
    digest=hashlib.sha256(str(work['revision']).encode()+b'\0'+plan+b'\0'+json.dumps(contract,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return path,contract,digest


def linked_tasks(store, work):
    return [t for t in store.list() if t.get('work_id')==work['id']]


def idle(store,work):
    if any(t['state'] in ACTIVE for t in linked_tasks(store,work)):
        raise ValueError('active or uncertain writer retained; hand off and verify stop first')


def check_task(config,store,task):
    required=config.get('require_design_approval',False) and task['role']!='leader'
    if not task.get('work_id'):
        if required:
            raise ValueError('product execution requires linked approved work')
        return None
    work=get(store,task['work_id'])
    if work['team']!=task['team'] or Path(work['root']).resolve()!=Path(task['root']).resolve():
        raise ValueError('execution does not match product work scope')
    _,contract,digest=document(config,work)
    if task['role']=='leader':
        if task.get('classification',{}).get('kind')!='design' or work['phase']!='planning' or task.get('design_revision')!=work['revision']:
            raise ValueError('linked leader work must be classified as design')
        return work
    approval=work.get('approval')
    if work['phase'] not in ('approved','in_progress','awaiting_review') or not approval or approval['digest']!=digest or task.get('design_digest')!=digest:
        raise ValueError('current design revision is not approved for this execution')
    if work.get('checkpoint'):
        point=work['checkpoint']
        if git(work['root'],'rev-parse','HEAD').decode().strip()!=point['head'] or fingerprint(work['root'])!=point['fingerprint']:
            raise ValueError('restart state changed; inspect and reconcile before execution')
    if subprocess.run(['git','-C',work['root'],'merge-base','--is-ancestor',approval['base'],task['base']],capture_output=True).returncode:
        raise ValueError('execution base is outside approved history')
    scope_check(work,contract)
    return work


def scope_check(work,contract):
    changed=git(work['root'],'diff','--no-renames','--name-only','-z',work['approval']['base']).split(b'\0')
    changed+=git(work['root'],'diff','--cached','--no-renames','--name-only','-z',work['approval']['base']).split(b'\0')
    changed+=git(work['root'],'ls-files','--others','--exclude-standard','-z').split(b'\0')
    for value in filter(None,changed):
        name=value.decode('utf-8','surrogateescape')
        if not any(name==p or p.endswith('/') and name.startswith(p) for p in contract['allowed_paths']):
            raise ValueError('repository change is outside approved design scope')


def run(args,config,store):
    setup(store)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.work):
        raise ValueError('invalid product work ID')
    if args.action=='create':
        root=Path(args.root).resolve()
        team=config['teams'][args.team]
        if not any(root.is_relative_to(Path(p).resolve()) for p in [team['root'],*config.get('shared_roots',[])]):
            raise ValueError('work root is outside configured roots')
        if git(root,'rev-parse','--show-toplevel').decode().removesuffix('\n')!=str(root):
            raise ValueError('work root must identify a complete Git worktree')
        contract=json.loads(Path(args.contract_file).read_text());validate_contract(contract)
        outside_git(Path(args.plan_file).resolve().parent)
        outside_git(Path(args.contract_file).resolve().parent)
        plan=Path(args.plan_file).read_bytes()
        if not plan.strip():raise ValueError('empty design')
        private=private_root(config,create=True)
        with store.transaction():
            if store.db.execute('SELECT 1 FROM work_items WHERE id=?',(args.work,)).fetchone():
                raise ValueError('work already exists; inspect instead of recreating')
            work_dir=private/args.work
            # A pre-existing directory or symlink is an uncertain prior attempt.
            # Never follow it while creating private revision files.
            work_dir.mkdir(mode=0o700)
            path=work_dir/'r1';path.mkdir(mode=0o700)
            outside_git(path)
            for filename,data in (('plan.md',plan),('contract.json',json.dumps(contract,ensure_ascii=False,indent=2).encode())):
                fd=os.open(path/filename,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
            work={'private_root':str(private),'id':args.work,'team':args.team,'root':str(root),'revision':1,'phase':'planning','title':contract['title'],'steps':contract['steps'],'milestones':{},'created_at':time.time()}
            return save(store,work,'work_created')
    with store.transaction():
        work=get(store,args.work)
        if args.action=='show':return work
        if args.action=='submit':
            idle(store,work)
            if work['phase'] not in ('planning','awaiting_approval'):raise ValueError('create a new revision before changing approved design')
            path,contract,digest=document(config,work)
            work.update(phase='awaiting_approval',submitted_digest=digest,title=contract['title'],steps=contract['steps'])
            work.pop('hold',None)
            return save(store,work,'design_submitted')
        if args.action=='approve':
            idle(store,work)
            path,contract,digest=document(config,work)
            if (getattr(args,'expected_revision',None) is not None and args.expected_revision!=work['revision']
                    or getattr(args,'expected_digest',None) is not None and args.expected_digest!=digest):
                raise ValueError('selected approval revision/digest changed; read the design again')
            if work['phase']!='awaiting_approval' or work.get('submitted_digest')!=digest or not args.user_approved or not args.approval_reference.strip():
                raise ValueError('explicit approval must match the submitted revision')
            with revision_guard(work['root']):
                if git(work['root'],'status','--porcelain=v1').strip():raise ValueError('approve against a clean isolated worktree')
                work.update(phase='approved',approval={'digest':digest,'reference':args.approval_reference,'at':time.time(),
                            'base':git(work['root'],'rev-parse','HEAD').decode().strip(),'revision':work['revision']})
                work.update(steps=contract['steps'],title=contract['title'])
                work.pop('hold',None)
            return save(store,work,'design_approved')
        if args.action=='revise':
            idle(store,work)
            old,_,_=document(config,work)
            new=old.parent/('r'+str(work['revision']+1));new.mkdir(mode=0o700)
            for filename in ('plan.md','contract.json'):
                fd=os.open(new/filename,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as out:out.write((old/filename).read_bytes());out.flush();os.fsync(out.fileno())
            work.update(revision=work['revision']+1,phase='planning',milestones={})
            for key in ('approval','submitted_digest','checkpoint','review_task','hold'):work.pop(key,None)
            return save(store,work,'design_revision_created')
        if args.action=='checkpoint':
            idle(store,work)
            evidence=json.loads(Path(args.evidence_file).read_text())
            if not strings(evidence.get('verification')) or not isinstance(evidence.get('next'),str) or not evidence['next'].strip():
                raise ValueError('restart checkpoint requires verification and next action')
            _,_,digest=document(config,work)
            with revision_guard(work['root']):
                work['checkpoint']={'head':git(work['root'],'rev-parse','HEAD').decode().strip(),
                                    'fingerprint':fingerprint(work['root']),'design_digest':digest,
                                    'verification':evidence['verification'],'next':evidence['next'],'at':time.time()}
            return save(store,work,'work_checkpointed')
        if args.action=='record':
            _,contract,digest=document(config,work)
            if work['phase'] not in ('approved','in_progress','awaiting_review') or work['approval']['digest']!=digest:
                raise ValueError('milestones require unchanged approved design')
            if args.step not in {s['id'] for s in contract['steps']}:raise ValueError('unknown approved step')
            evidence=json.loads(Path(args.evidence_file).read_text())
            if not strings(evidence.get('verification')):raise ValueError('step requires verification evidence')
            scope_check(work,contract)
            work['milestones'][args.step]={'verification':evidence['verification'],
                'evidence_sha256':hashlib.sha256(Path(args.evidence_file).read_bytes()).hexdigest(),
                'head':git(work['root'],'rev-parse','HEAD').decode().strip(),'fingerprint':fingerprint(work['root']),'at':time.time()}
            work['phase']='awaiting_review' if len(work['milestones'])==len(contract['steps']) else 'in_progress'
            return save(store,work,'milestone_recorded')
        if args.action=='finish':
            idle(store,work)
            _,contract,digest=document(config,work)
            if not work.get('approval') or digest!=work['approval']['digest'] or work['phase']!='awaiting_review' or set(work['milestones'])!={s['id'] for s in contract['steps']}:
                raise ValueError('all approved steps and independent review required')
            reviewer=store.get(args.review_task)
            if reviewer.get('work_id')!=work['id'] or reviewer.get('design_digest')!=digest or reviewer['role']!='reviewer' or reviewer['state']!='awaiting_review' or reviewer.get('review_verdict')!='pass' or not reviewer.get('runtime',{}).get('identity_checked'):
                raise ValueError('independent review does not match this approved work')
            if any(t.get('owner')==reviewer.get('owner') or t.get('runtime',{}).get('name')==reviewer.get('runtime',{}).get('name') for t in linked_tasks(store,work) if t['role']=='implementer'):
                raise ValueError('review must be separate from implementation')
            with revision_guard(work['root']):
                head=git(work['root'],'rev-parse','HEAD').decode().strip()
                if git(work['root'],'status','--porcelain=v1').strip() or reviewer['head']!=head or reviewer['base']!=head:
                    raise ValueError('review is stale or worktree changed')
                reviews=[t for t in linked_tasks(store,work) if t['role']=='reviewer' and t.get('design_digest')==digest and t['base']==head]
                if any(t['state']!='awaiting_review' for t in reviews):
                    raise ValueError('an applicable review is pending or unresolved')
                completed=[(max(e['seq'] for e in store.events(t['id']) if e['event']=='awaiting_review'),t['id']) for t in reviews]
                if not completed or max(completed)[1]!=reviewer['id']:
                    raise ValueError('a newer applicable review must be resolved first')
                current=fingerprint(work['root'])
                if any(m.get('head')!=head or m.get('fingerprint')!=current for m in work['milestones'].values()):
                    raise ValueError('source changed after step verification; rerun verification and record affected steps')
                scope_check(work,contract)
                work.update(phase='complete',review_task=reviewer['id'],completed_head=head)
            return save(store,work,'work_verified')
        raise ValueError('unknown workflow action')


def snapshot(state):
    """Read-only: a status refresh never creates a DB or starts a model."""
    path=Path(state).expanduser().resolve()
    if not path.is_file():return {'works':[],'tasks':[],'quotas':{},'monitor':None,'event_sequence':0,'observed_at':time.time()}
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN')
        tasks=[json.loads(r[0]) for r in db.execute('SELECT body FROM tasks')]
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='work_items'").fetchone()
        works=[json.loads(r[0]) for r in db.execute('SELECT body FROM work_items')] if exists else []
        events=db.execute('SELECT MAX(seq) FROM events').fetchone()[0] or 0
        quotas={k:json.loads(v) for k,v in db.execute('SELECT profile,body FROM quotas')}
    from .monitor import cache_path
    monitor=None
    try:
        cached=cache_path(state)
        if cached.stat().st_size<=1024*1024:monitor=json.loads(cached.read_text())
    except (OSError,ValueError):pass
    observations={}
    observed=path.with_name(path.name+'.usage-observations.json')
    try:
        if observed.stat().st_size<=1024*1024:
            value=json.loads(observed.read_text())
            if isinstance(value,dict):observations=value
    except (OSError,ValueError):pass
    return {'works':works,'tasks':tasks,'quotas':quotas,'monitor':monitor,'usage_observations':observations,'event_sequence':events,'observed_at':time.time()}
