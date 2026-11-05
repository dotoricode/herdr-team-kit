"""Independent review must consume only the exact host-collected parent evidence."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_operations as fixtures
from teamkit import operations
from teamkit.config import binding,load,profile
from teamkit.prompt_state import empty_input
from teamkit.tickets import TicketStore


class ReviewContractTests(unittest.TestCase):
    setUp=fixtures.OperationsTest.setUp
    enqueue=fixtures.OperationsTest.enqueue

    def parent(self):
        worker=self.root/'worker';worker.mkdir()
        config=json.loads(self.config.read_text());config['teams']['example']['root']=str(worker)
        config['operations']['allocation_policy']='usage';self.config.write_text(json.dumps(config))
        parent=self.enqueue('PARENT',review_required=True,scope=dict(paths=[str(worker)],actions=getattr(self,'parent_actions',['read']),data=['fixture']),
            resources=[dict(kind='path',name=str(worker),mode='read')])
        queue=TicketStore(self.state);self.addCleanup(queue.close)
        actor=dict(self.runtime.executor,root=str(worker),task='fixture',runtime={'name':'fixture'},profile_binding=binding(profile(load(self.config),'codex','personal')))
        attempt=queue.reserve('PARENT',actor,{},self.runtime.observation())
        queue.receipt('PARENT',attempt['id'],actor['owner'],actor['terminal'],dict(actor,turn=1))
        report=self.root/'operation-results/PARENT/host-artifact';report.parent.mkdir(parents=True);report.write_text('Verified fixture parent result')
        queue.submit('PARENT',attempt['id'],actor['owner'],dict(artifact=str(report),writer_stopped=True))
        attempt=queue.attempt(attempt['id'])
        child=operations.followup(queue,queue.get('PARENT'),attempt,'required','Review the exact parent result.',reviewer=True)
        return queue,actor,child,report

    def test_review_child_authorizes_and_stages_exact_host_artifact_readonly(self):
        queue,author,child,report=self.parent()
        self.assertEqual('pending',child['state'])
        self.assertIn(dict(kind='path',name=str(report),mode='read'),child['resources'])
        reviewer=dict(author,terminal='t2',pid=43,owner='reviewer')
        attempt=queue.reserve(child['id'],reviewer,{},self.runtime.observation())
        operations.prompt(self.config,self.state,child,attempt)
        contract=json.loads((Path(attempt['mailbox'])/'contract.json').read_text())
        entry=next(x for x in contract['staged_inputs'] if x['original']==str(report))
        self.assertEqual(report.read_text(),Path(entry['staged']).read_text())
        self.assertEqual(0,Path(entry['staged']).stat().st_mode & 0o222)

    def test_review_artifact_read_grant_cannot_widen_to_write_or_its_directory(self):
        import copy
        self.parent_actions=['read','write']
        queue,author,child,report=self.parent()
        for index,(path,mode) in enumerate(((str(report),'write'),(str(report.parent),'read'))):
            spec={k:copy.deepcopy(child[k]) for k in ('product','role','kind','goal','completion','checks','verification_commands','profiles','scope','resources','parent_id','parent_attempt','relation')}
            spec['id']='UNAPPROVED-'+str(index)
            spec['scope']['paths']=[str(self.root/'worker'),path]
            spec['scope']['actions']=['read','write'] if mode=='write' else ['read']
            spec['resources']=[dict(kind='path',name=path,mode=mode)]
            self.assertEqual('waiting_approval',queue.enqueue(spec,spec['id'])['state'])

    def test_restarted_author_terminal_cannot_be_its_own_review_executor(self):
        queue,author,child,report=self.parent()
        runtime=operations.NativeRuntime(load(self.config),self.state)
        restarted=dict(author,owner='fresh-owner',pid=44)
        with patch.object(runtime,'native',return_value={}),patch('teamkit.providers.identity'):
            issues=runtime.admit_report(child,restarted)
        self.assertIn('review_independence',[x['code'] for x in issues])

    def test_percentage_multiline_drafts_remain_input_including_blank_lines(self):
        for text in ('› \n  increase threshold by 10%\n',
                     '❯ \n  keep quota 80% and wait\n',
                     '› \n\n  draft 10%\n\n  Arbitrary native footer'):
            with self.subTest(text=text):self.assertFalse(empty_input(text))
