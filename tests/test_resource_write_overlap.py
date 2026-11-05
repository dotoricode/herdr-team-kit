import tempfile
import time
import unittest
from pathlib import Path

from teamkit.resources import ResourceLedger, ResourceUnavailable, _conflict


class WriteOverlapRule(unittest.TestCase):
    """Only two overlapping writes conflict; a read never blocks or is blocked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.ledger = ResourceLedger(self.root / 'state.sqlite', clock=lambda: self.now)

    def path(self, name, mode):
        return {'kind': 'path', 'name': str(self.root / name), 'mode': mode}

    def named(self, name, mode):
        return {'kind': 'named', 'name': name, 'mode': mode}

    def reserve(self, attempt, resources):
        return self.ledger.reserve(attempt, 'T-' + attempt, 'codex/team', resources, None,
                                   {'observed_at': self.now, 'complete': True, 'terminals': []})

    def test_read_never_conflicts_with_read_or_write_on_the_same_or_nested_path(self):
        for left in ('read', 'write'):
            for right in ('read', 'write'):
                if left == right == 'write':
                    continue
                for other in ('repo', 'repo/sub'):
                    with self.subTest(left=left, right=right, other=other):
                        self.assertFalse(_conflict(self.path('repo', left), self.path(other, right)))
                        self.assertFalse(_conflict(self.path(other, right), self.path('repo', left)))

    def test_write_write_conflicts_for_same_ancestor_and_descendant_but_not_siblings(self):
        self.assertTrue(_conflict(self.path('repo', 'write'), self.path('repo', 'write')))
        self.assertTrue(_conflict(self.path('repo', 'write'), self.path('repo/sub', 'write')))
        self.assertTrue(_conflict(self.path('repo/sub', 'write'), self.path('repo', 'write')))
        self.assertFalse(_conflict(self.path('repo/a', 'write'), self.path('repo/b', 'write')))

    def test_named_resource_conflicts_only_for_write_write_on_the_same_name(self):
        self.assertTrue(_conflict(self.named('device:ios-1', 'write'), self.named('device:ios-1', 'write')))
        self.assertFalse(_conflict(self.named('device:ios-1', 'write'), self.named('device:ios-1', 'read')))
        self.assertFalse(_conflict(self.named('device:ios-1', 'read'), self.named('device:ios-1', 'read')))
        self.assertFalse(_conflict(self.named('device:ios-1', 'write'), self.named('device:ios-2', 'write')))

    def test_reviewer_reading_a_root_runs_beside_a_reviewer_writing_a_sibling_report(self):
        # The reviewed root is read by both, each writes its own report folder under a sibling directory.
        self.reserve('one', [self.path('product', 'read'), self.path('reports/one', 'write')])
        self.reserve('two', [self.path('product', 'read'), self.path('reports/two', 'write')])

    def test_a_reader_runs_beside_a_writer_of_the_same_tree_in_both_orders(self):
        self.reserve('writer', [self.path('product', 'write')])
        self.reserve('reader', [self.path('product', 'read')])
        self.reserve('reader-sub', [self.path('product/sub', 'read')])

    def test_overlapping_writes_still_wait_even_when_a_read_is_also_declared(self):
        self.reserve('one', [self.path('product', 'read'), self.path('reports/shared', 'write')])
        with self.assertRaises(ResourceUnavailable) as caught:
            self.reserve('two', [self.path('product', 'read'), self.path('reports/shared/inner', 'write')])
        self.assertEqual(caught.exception.code, 'resource_conflict')


if __name__ == '__main__':
    unittest.main()
