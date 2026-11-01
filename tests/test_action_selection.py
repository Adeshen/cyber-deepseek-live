"""Action recognition and selection use local data, with no cloud calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from src import dm_router


class ActionSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router=dm_router.DanmakuRouter()
        self.router._conf_get=lambda:{'enabled':True,'mode':'auto','cooldown_s':0,
            'same_action_windows':[8,12,16],'selection_per_tick':1,'selection_max_age_s':10}
        self.router._load_aliases()

    async def test_common_requests_and_canonical_names_use_local_matching(self):
        examples={'比个心':'对镜头比心','飞吻':'飞吻','挥挥手':'挥手打招呼',
                  '眨一下眼':'对镜头眨眼','点点头':'轻轻点头','笑一个':'露出笑容',
                  '拍拍手':'轻轻鼓掌','戴上墨镜':'拿出墨镜戴上'}
        self.router.do_fire=AsyncMock(return_value=True)
        self.router._t2=AsyncMock(side_effect=AssertionError('local actions need no LLM'))
        for phrase,target in examples.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(self.router.match(phrase),target)
                self.assertEqual(await self.router._process_one('测试',phrase),('fire',False))
                self.assertEqual(self.router.do_fire.await_args.args,(target,'t0'))
        self.router._t2.assert_not_awaited()

    def test_compiler_uses_local_phrases_when_cloud_catalog_is_empty(self):
        self.assertFalse(self.router._names)
        prompt=self.router._compiler_prompt()
        for phrase in ['挥手打招呼','飞吻','托腮发呆']:
            self.assertIn(phrase,prompt)
        self.assertNotIn('(清单未加载)',prompt)

    def test_fresh_varied_action_wins_over_repeat_votes(self):
        self.router._last_fired_text='对镜头比心'
        items=[{'content':'比个心','ts':999.9} for _ in range(5)]
        fresh={'content':'挥挥手','ts':999.0}
        with patch('src.dm_router._now',return_value=1000):
            selected=self.router._select_inbox(items+[fresh])
        self.assertEqual(selected,[fresh])

    def test_stale_and_chat_messages_are_not_action_candidates(self):
        items=[{'content':'比个心','ts':980},{'content':'今天下雨了','ts':999}]
        with patch('src.dm_router._now',return_value=1000):
            self.assertEqual(self.router._select_inbox(items),[])
        self.assertEqual(self.router._selection_stale,1)

    def test_aliases_of_one_action_do_not_fill_multiple_slots(self):
        self.router._conf_get=lambda:{'selection_per_tick':3}
        with patch('src.dm_router._now',return_value=1000):
            selected=self.router._select_inbox([{'content':'比心','ts':999},
                {'content':'比个心','ts':999.1},{'content':'飞吻','ts':999.2}])
        self.assertEqual(len(selected),2)

    def test_alias_edits_are_loaded_without_restarting_router(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'aliases.json'
            path.write_text(json.dumps({'挥手打招呼':['挥挥手']}),encoding='utf-8')
            with patch('src.dm_router.ALIAS_PATH',path):
                self.router._load_aliases()
                stamp=path.stat().st_mtime_ns
                path.write_text(json.dumps({'轻轻点头':['点点头']}),encoding='utf-8')
                os.utime(path,ns=(stamp+1000000,stamp+1000000))
                self.assertEqual(self.router.match('点点头'),'轻轻点头')
