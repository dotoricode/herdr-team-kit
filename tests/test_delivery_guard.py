import unittest
from unittest.mock import patch

from teamkit.core_runtime import CoreRuntime


class DeliveryGuardTest(unittest.TestCase):
    def test_guard_after_last_native_observation_can_prevent_actual_send(self):
        native={'address':'t1','status':{'state':'idle'},'foreground':{'pid':42},'turn':1}
        order=[]
        actor=CoreRuntime()
        def observed(*args,**kwargs):
            order.append('native')
            return native
        def reject():
            order.append('guard')
            raise ValueError('approval changed')
        with patch('teamkit.core_runtime.require_managed',return_value='t0'), patch.object(actor,'_native',side_effect=observed), patch('teamkit.core_runtime._call',side_effect=AssertionError('no external delivery')):
            with self.assertRaisesRegex(ValueError,'approval changed'):
                actor.accept('fixture','bounded fixture',before_send=reject)
        self.assertEqual(['native','guard'],order)
