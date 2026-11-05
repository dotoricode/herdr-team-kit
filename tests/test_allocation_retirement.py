"""Retired pool records cannot hold a current native session or capacity."""
import json
import unittest
from unittest.mock import patch
import test_admission as admission_fixtures
import test_operations as operation_fixtures
import test_allocation_mailbox as mailbox_fixtures
from teamkit import admission, operations
from teamkit.resources import ResourceLedger
from teamkit.tickets import TicketStore
from teamkit.store import Store


class DeadLegacyTests(unittest.TestCase):
    setUp=admission_fixtures.AdmissionTests.setUp
    create=admission_fixtures.AdmissionTests.create
    claim=admission_fixtures.AdmissionTests.claim
    make_running=admission_fixtures.AdmissionTests.make_running
    observation=admission_fixtures.AdmissionTests.observation

    def test_dead_legacy_process_releases_execution_writes_and_synthetic_capacity(self):
        self.create('OLD','implementer',self.path.parent/'shared')
        admission.configure(self.store.db,{'global_limit':1})
        with patch.object(admission,'observe_native',return_value=self.observation()):self.make_running('OLD')
        native=dict(address='OLD',state='live',foreground={'pid':12,'name':'codex'},labels={'teamkit.owner':'fresh'},status={'state':'idle','app':'codex'})
        with patch.object(admission,'native_get',return_value=native),patch.object(admission.os,'kill',side_effect=ProcessLookupError):
            self.assertEqual(1,len(admission.reconcile_stopped(self.store.db)))
            row=ResourceLedger._rows(self.store.db)[0]
            self.assertFalse(row['execution_reserved']);self.assertFalse(row['writes_held'])
            self.assertEqual(1,admission.snapshot(self.path,native_observation=self.observation())['available'])
            self.create('NEXT','implementer',self.path.parent/'shared')
            with patch.object(admission,'observe_native',return_value=self.observation()):self.claim('NEXT')


class RetirementTests(unittest.TestCase):
    setUp=operation_fixtures.OperationsTest.setUp
    enqueue=operation_fixtures.OperationsTest.enqueue
    file_worker=mailbox_fixtures.MailboxTests.file_worker

    def test_general_candidates_never_read_the_static_manifest(self):
        TicketStore(self.state).close();Store(self.state).close()
        runtime=operations.NativeRuntime(json.loads(self.config.read_text()),self.state)
        with patch('teamkit.session_pool._read',side_effect=AssertionError('retired manifest must not be consulted')):
            self.assertEqual([],runtime.executors())

    def test_explicit_team_pull_collects_previous_completed_turn_before_candidate_admission(self):
        self.enqueue('OLD');self.file_worker(missing=True)
        operations.tick(self.config,self.state,self.runtime)
        self.enqueue('NEXT');self.turn=2
        config=json.loads(self.config.read_text());config['operations']['allocation_policy']='usage';self.config.write_text(json.dumps(config))
        self.runtime.send=lambda actor,text:dict(accepted=True,terminal=actor['terminal'],native_pid=actor['pid'],turn_baseline=2,turn=2)
        with patch('teamkit.allocation.refresh_usage',return_value=None):
            result=operations.pull(self.config,self.state,'example',ticket='NEXT',runtime=self.runtime)
        self.assertEqual(['NEXT'],[x['ticket'] for x in result['started']])
