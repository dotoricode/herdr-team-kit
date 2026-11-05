import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teamkit.knowledge import search
from teamkit.knowledge_hub import snapshot


class KnowledgeHubTests(unittest.TestCase):
    def test_roster_matches_app_sessions_not_hidden_task_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.sqlite'
            config={'teams':{'alpha':{'root':'/example/alpha','roles':{}}},'profiles':{'codex':{'team':{}}}}
            binding={'teams':{'alpha':{'terminal':'t1','space':'s1'}}}
            app={'spaces':[{'id':'s1','name':'Alpha','tabs':[{'name':'직원','terminals':[{'terminal':'t2','app':'codex','status':'idle'}]}]}]}
            with patch('teamkit.knowledge_hub.load',return_value=config),patch('teamkit.knowledge_hub.bindings',return_value=binding),patch('teamkit.knowledge_hub.app_sessions',return_value=app,create=True),patch('teamkit.knowledge_hub.readonly_snapshot',return_value={'tasks':[]}),patch('teamkit.knowledge_hub.session_read',return_value={'state':'live','labels':{'ui.display-name':'샘플 직원'}},create=True):
                result=snapshot('config',state,'binding')['teams'][0]
            self.assertEqual([x['terminal'] for x in result['employees']],['t2'])
            self.assertEqual(result['employees'][0]['label'],'샘플 직원')
            self.assertEqual(result['terminal'],'t2')
    def test_search_uses_document_text_and_returns_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'alpha.md';path.write_text('# 팀 주의사항\n\n샘플 빌드 확인\n',encoding='utf-8')
            self.assertEqual(search(tmp,'빌드')[0]['source'],str(path))
            self.assertEqual(search(tmp,'없는 단어'),[])

    def test_roster_writes_only_verified_identity_and_preserves_on_read_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.sqlite'
            config={'teams':{'alpha':{'roles':{'leader':{'provider':'codex'}}}},'profiles':{'codex':{'team':{}}}}
            binding={'teams':{'alpha':{'label':'Alpha','terminal':'t1','space':'s1'}}}
            tasks={'tasks':[{'id':'task1','owner':'owner','team':'alpha','role':'leader','provider':'codex','account':'team','runtime':{'terminal':'t1'}}]}
            native={'spaces':[{'id':'s1','name':'Alpha','tabs':[{'name':'직원','terminals':[{'terminal':'t1','app':'codex','status':'working'}]}]}]}
            with patch('teamkit.knowledge_hub.load',return_value=config),patch('teamkit.knowledge_hub.bindings',return_value=binding),patch('teamkit.knowledge_hub.app_sessions',return_value=native),patch('teamkit.knowledge_hub.readonly_snapshot',return_value=tasks),patch('teamkit.knowledge_hub.verified_session',return_value=(False,{})),patch('teamkit.knowledge_hub.session_read',return_value={'labels':{'ui.display-name':'Alpha 직원'}}):
                result=snapshot('config',state,'binding')
                self.assertEqual(len(result['teams'][0]['employees']),1)
                self.assertFalse(result['teams'][0]['employees'][0]['identity_verified'])
                self.assertIsNone(result['teams'][0]['employees'][0]['account'])
                target=Path(tmp)/'knowledge-hub-roster.json';saved=target.read_bytes()
            with patch('teamkit.knowledge_hub.load',return_value=config),patch('teamkit.knowledge_hub.bindings',return_value=binding),patch('teamkit.knowledge_hub.app_sessions',side_effect=OSError('offline')):
                with self.assertRaises(OSError):snapshot('config',state,'binding')
                self.assertEqual(target.read_bytes(),saved)
            self.assertEqual(json.loads(saved)['teams'][0]['label'],'Alpha')


if __name__ == '__main__':
    unittest.main()
