import unittest
from unittest.mock import patch

from teamkit.staff import display_name
from teamkit.operations import NativeRuntime


class StaffDisplayTest(unittest.TestCase):
    def test_product_profile_aliases_are_stable_without_team_or_role_prefix(self):
        self.assertEqual(display_name('claude', 'team', product='alpha'), '김척척 (팀)')
        self.assertEqual(display_name('claude', 'personal', product='beta', aliases={'beta': {'claude/personal': '강든든'}}), '강든든 (개인)')
        self.assertEqual(display_name('codex', 'team', product='gamma', aliases={'gamma': {'codex/team': '한반듯'}}), '한반듯 (팀)')
        self.assertEqual(display_name('codex', 'personal', product='delta', aliases={'delta': {'codex/personal': '황알뜰'}}), '황알뜰 (개인)')
        self.assertEqual(display_name('codex', 'personal', role='coordinator'), '도차곡 (개인)')
        self.assertEqual(display_name('codex', 'personal', role='ui'), '유깐깐 (개인)')

    def test_worker_owner_alias_is_stable_and_separate_from_product_pool(self):
        name = display_name('claude', 'team', product='alpha', owner='example-owner')
        self.assertEqual(name, display_name('claude', 'team', product='beta', owner='example-owner'))
        self.assertNotEqual(name, '김척척 (팀)')
        self.assertTrue(name.endswith(' (팀)'))
        self.assertNotIn('클로드', name)
        self.assertNotIn('코덱스', name)

    def test_unknown_identity_is_explicit(self):
        self.assertEqual(display_name('unknown', 'unknown'), '직원 (계정 미확인)')

    def test_ticket_worker_launch_uses_alias_without_changing_executor(self):
        runtime = NativeRuntime({'operations': {'spaces': {'qa': 'qa-space'}}}, '/example/state.sqlite')
        executor = {'terminal': 'example-terminal', 'account': 'personal', 'provider': 'codex', 'owner': 'example-owner'}
        with patch('teamkit.workers.launch', return_value=executor), \
                patch('teamkit.core_runtime._call') as call, \
                patch.object(runtime, 'admit'), \
                patch.object(runtime, 'native', return_value={'turn': 1}), \
                patch('teamkit.operations.time.sleep'):
            result = runtime.launch('/example/config.json',
                                    {'id': 'EXAMPLE', 'role': 'reviewer', 'product': 'alpha'}, {})
        self.assertIs(result, executor)
        call.assert_called_once_with('label', 'example-terminal', 'operations.ticket=EXAMPLE',
                                     'ui.display-name='+display_name('codex', 'personal', owner='example-owner'),
                                     'herdr.space=qa-space')
