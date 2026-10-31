from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

from test_cli import Fixture


class InspectionTests(Fixture):
    def seed_work(self):
        self.create()
        with closing(sqlite3.connect(self.state,isolation_level=None)) as db:
            db.execute('CREATE TABLE work_items (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('INSERT INTO work_items VALUES (?,?)', ('W1',json.dumps({'id':'W1','phase':'planning'})))

    def fingerprint(self):
        return {p.name:(stat.S_IMODE(p.stat().st_mode),p.stat().st_mtime_ns,
                        hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.state.parent.iterdir() if p.is_file()}

    def test_four_queries_preserve_ledger_bytes_modes_and_directory(self):
        self.seed_work()
        for args in (('show','T1'),('list',),('events','T1'),('work','show','W1')):
            with self.subTest(args=args):
                self.state.chmod(0o640)
                before=self.fingerprint()
                result=self.call(*args)
                self.assertTrue(result)
                self.assertEqual(self.fingerprint(),before)

    def test_missing_ledger_is_explicit_and_never_created(self):
        for index,args in enumerate((('show','T1'),('list',),('events','T1'),('work','show','W1'))):
            with self.subTest(args=args):
                self.state=self.root/'missing'/str(index)/'state.sqlite'
                with self.assertRaisesRegex(ValueError,'ledger does not exist'):
                    self.call(*args)
                self.assertFalse(self.state.parent.exists())

    def test_missing_work_table_does_not_migrate_the_ledger(self):
        self.create();before=self.fingerprint()
        with self.assertRaisesRegex(ValueError,'unknown product work'):
            self.call('work','show','W1')
        with closing(sqlite3.connect(self.state,isolation_level=None)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='work_items'").fetchone())
        self.assertEqual(self.fingerprint(),before)

    def test_unknown_ids_preserve_query_results(self):
        self.seed_work()
        with self.assertRaisesRegex(ValueError,'unknown task'):self.call('show','MISSING')
        self.assertEqual(self.call('events','MISSING'),[])
        with self.assertRaisesRegex(ValueError,'unknown product work'):self.call('work','show','MISSING')
        self.assertEqual([v['id'] for v in self.call('list')],['T1'])
        with self.assertRaisesRegex(ValueError,'invalid product work ID'):self.call('work','show','bad/id')

    def test_corrupt_database_returns_json_error_without_traceback(self):
        self.state.parent.mkdir();self.state.write_bytes(b'not a sqlite database')
        before=self.fingerprint()
        cli=Path(__file__).resolve().parents[1]/'teamkit_cli.py'
        result=subprocess.run([sys.executable,str(cli),'--config',str(self.config),'--state',str(self.state),'show','T1'],capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertIn('cannot read task ledger',json.loads(result.stderr)['error'])
        self.assertNotIn('Traceback',result.stderr)
        self.assertEqual(self.fingerprint(),before)

    def test_invalid_record_bodies_are_clear_errors(self):
        self.seed_work()
        with closing(sqlite3.connect(self.state,isolation_level=None)) as db:
            db.execute("UPDATE tasks SET body='{' WHERE id='T1'")
            db.execute("UPDATE events SET body='[]'")
            db.execute("UPDATE work_items SET body='7' WHERE id='W1'")
        for args,label in ((('show','T1'),'task'),(('list',),'task'),(('events','T1'),'event'),(('work','show','W1'),'product work')):
            with self.subTest(args=args),self.assertRaisesRegex(ValueError,'invalid '+label+' record'):
                self.call(*args)

    def test_exclusive_writer_reports_read_failure(self):
        self.create()
        writer=sqlite3.connect(self.state,isolation_level=None)
        try:
            writer.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(ValueError,'cannot read task ledger.*locked'):
                self.call('show','T1')
        finally:
            writer.rollback();writer.close()

    def test_reserved_writer_allows_committed_snapshot_reads(self):
        self.seed_work()
        writer=sqlite3.connect(self.state,isolation_level=None)
        try:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE work_items SET body=? WHERE id='W1'",(json.dumps({'id':'W1','phase':'uncommitted'}),))
            self.assertEqual(self.call('show','T1')['state'],'queued')
            self.assertEqual(self.call('work','show','W1')['phase'],'planning')
            self.assertEqual([v['id'] for v in self.call('list')],['T1'])
            self.assertEqual(self.call('events','T1')[0]['event'],'created')
        finally:
            writer.rollback();writer.close()

    def test_ledger_uri_special_characters_are_read_literally(self):
        self.state=self.root/'ledger ?#%'/'state ?#%.sqlite'
        self.create()
        self.assertEqual(self.call('show','T1')['id'],'T1')

    def test_wal_is_rejected_before_creating_or_touching_side_files(self):
        self.seed_work()
        for keep_writer in (False,True):
            with self.subTest(keep_writer=keep_writer):
                writer=sqlite3.connect(self.state,isolation_level=None)
                self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
                if not keep_writer:writer.close()
                before=self.fingerprint()
                try:
                    for args in (('show','T1'),('list',),('events','T1'),('work','show','W1')):
                        with self.assertRaisesRegex(ValueError,'WAL'):
                            self.call(*args)
                        self.assertEqual(self.fingerprint(),before)
                finally:
                    if keep_writer:writer.close()
