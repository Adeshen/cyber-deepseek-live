"""Structured follow/gift routing; no real sessions or provider requests."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


class NotificationRoutingTests(unittest.IsolatedAsyncioTestCase):
    def handler(self):
        engine = SimpleNamespace(enqueue_danmaku=Mock(return_value={"queued": True}),
                                 enqueue_gift=AsyncMock(return_value={"queued": True}), tr_add=Mock(),
                                 note_gift_event=Mock(return_value=True))
        router = SimpleNamespace(looks_like_cmd=Mock(side_effect=AssertionError("notification is not an action")))
        path = Path(__file__).resolve().parents[1] / 'src/bridge.py'
        module = ast.parse(path.read_text(encoding='utf-8'))
        handler = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'danmaku')
        handler.decorator_list = []
        namespace = {'engine': engine, 'dm_router': router, 'DanmakuBody': SimpleNamespace}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(path), 'exec'), namespace)
        return namespace['danmaku'], engine

    async def test_follow_uses_silent_priority_queue_without_action_matching(self):
        handler, engine = self.handler()
        await handler(SimpleNamespace(kind='follow', text='新关注', content='平台关注事件', nick='阿米',
                                      platform='抖音', event_id='event-1', user_id='user-1', room_id='111', event_ts=0))
        payload = engine.enqueue_danmaku.call_args
        self.assertEqual(payload.args, ('新关注', 'vip'))
        self.assertTrue(payload.kwargs['answer_only'])
        self.assertEqual(payload.kwargs['event_kind'], 'follow')
        self.assertEqual(payload.kwargs['event_id'], 'event-1')
        engine.enqueue_gift.assert_not_awaited()

    async def test_gift_keeps_name_quantity_and_value_in_existing_batcher(self):
        handler, engine = self.handler()
        await handler(SimpleNamespace(kind='gift', text='玫瑰', content='玫瑰', nick='阿米', say_nick='',
                                      gift_item='玫瑰', gift_num=3, gift_value=6, platform='抖音',
                                      gift_group_id='group', gift_total=3, event_id='gift-1', user_id='u', room_id='r'))
        engine.enqueue_gift.assert_awaited_once_with({'nick': '阿米', 'say_nick': '',
            'item': '玫瑰', 'num': 3, 'value': 6, 'platform': '抖音'})
        engine.enqueue_danmaku.assert_not_called()


class GiftReceiptTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from src.engine import Engine
        with patch('src.engine.appconf.load', return_value={'tts': {'engine': 'breeze'}}), \
                patch('src.engine.ttsmod.create_tts'):
            self.engine = Engine()

    def test_receipt_is_visible_before_any_speech(self):
        self.assertTrue(self.engine.note_gift_event({'nick': '阿米', 'gift_item': '小心心', 'gift_num': 2,
                                                   'event_id': 'gift-1', 'user_id': 'u', 'room_id': 'r'}))
        row = self.engine.transcripts[-1]
        self.assertEqual(row['role'], 'gift')
        self.assertEqual(row['text'], '🎁 阿米 送出 小心心 ×2')
        self.assertIsNone(self.engine._gift_task)
        self.assertEqual(self.engine._gift_sent, 0)

    def test_combo_updates_one_receipt_key_and_duplicate_is_ignored(self):
        gift = {'nick': '阿米', 'gift_item': '小心心', 'gift_num': 2, 'gift_total': 2,
                'gift_group_id': 'combo-1', 'event_id': 'one', 'user_id': 'u', 'room_id': 'r'}
        self.engine.note_gift_event(gift)
        key = self.engine.transcripts[-1]['event_key']
        self.assertFalse(self.engine.note_gift_event(gift))
        self.engine.note_gift_event({**gift, 'event_id': 'two', 'gift_num': 3, 'gift_total': 5})
        self.assertEqual(self.engine.transcripts[-1]['event_key'], key)
        self.assertEqual(self.engine.transcripts[-1]['text'], '🎁 阿米 送出 小心心 ×5')
        self.assertEqual(self.engine.tr_total, 2)
