import unittest
from teamkit.protocol import tool_permission_hint


class ProtocolHintTest(unittest.TestCase):
    def test_codex_hint_is_limited_to_protocol_and_preserves_review_policy(self):
        hint=tool_permission_hint('codex')
        self.assertIn('정확한 공식 수신·제출·분류·완료·반환 CLI 명령',hint)
        self.assertIn('require_escalated',hint)
        self.assertIn('거절하거나 도구 정책이 금지하면 중단',hint)
        self.assertIn('sandbox·프로필·승인 정책을 바꾸거나',hint)
        self.assertNotIn('danger-full-access',hint)
        self.assertEqual('',tool_permission_hint('claude'))
