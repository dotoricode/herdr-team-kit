import tempfile
from unittest import TestCase
from unittest.mock import patch

from teamkit.operations import NativeRuntime
from teamkit.runtime import RuntimeError as NativeError


class NativeWorkerReadinessTest(TestCase):
    def test_fresh_worker_initialization_can_settle_without_sending_a_task(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=NativeRuntime({'operations':{}},root+'/state.sqlite')
            executor={'terminal':'t1','owner':'owner','provider':'codex','account':'team'}
            def native(executor,require_idle=True):
                return {'status':'idle' if require_idle else 'working','turn':1}
            with patch('teamkit.workers.launch',return_value=executor), patch('teamkit.core_runtime._call',return_value={}) as call, patch.object(runtime,'admit',side_effect=[NativeError('Core agent is not idle and ready; inspect before sending'),executor]), patch.object(runtime,'native',side_effect=native), patch('teamkit.operations.time.sleep'):
                result=runtime.launch('config',{'id':'ONE','product':'example','role':'investigator'}, {})
            self.assertEqual(executor,result)
            self.assertTrue(all(args[0][0]=='label' for args in call.call_args_list))

    def test_fresh_worker_draft_or_identity_change_is_never_retried(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=NativeRuntime({'operations':{}},root+'/state.sqlite')
            executor={'terminal':'t1','owner':'owner','provider':'codex','account':'team'}
            with patch('teamkit.workers.launch',return_value=executor), patch('teamkit.core_runtime._call',return_value={}), patch.object(runtime,'admit',side_effect=ValueError('draft or dialog')) as admit:
                with self.assertRaisesRegex(ValueError,'draft'):
                    runtime.launch('config',{'id':'ONE','product':'example','role':'investigator'}, {})
            self.assertEqual(1,admit.call_count)
