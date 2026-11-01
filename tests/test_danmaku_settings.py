"""Settings changes reconnect only the danmaku source, with no real credentials."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


class DanmakuSettingsTests(unittest.IsolatedAsyncioTestCase):
    def routes(self, initial, disk=None):
        current = copy.deepcopy(initial)

        def patch_conf(updates):
            current.update(copy.deepcopy(updates))
            return copy.deepcopy(current)

        def reload_conf():
            current.clear()
            current.update(copy.deepcopy(disk))
            return current

        engine = SimpleNamespace(configure_tts=Mock(), start=AsyncMock(), stop=AsyncMock())
        source = SimpleNamespace(stop=AsyncMock(), start=AsyncMock())
        configuration = SimpleNamespace(load=lambda: current, patch=patch_conf,
                                        reload=reload_conf, public=lambda: copy.deepcopy(current))
        path = Path(__file__).resolve().parents[1] / 'src/bridge.py'
        module = ast.parse(path.read_text(encoding='utf-8'))
        routes = [node for node in module.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name in {'settings_post', 'settings_reload'}]
        for node in routes:
            node.decorator_list = []
        namespace = {'appconf': configuration, 'engine': engine, 'danmaku_source': source}
        exec(compile(ast.Module(body=routes, type_ignores=[]), str(path), 'exec'), namespace)
        return namespace, engine, source

    async def test_changing_room_reconnects_source_without_touching_live_session(self):
        routes, engine, source = self.routes({'danmaku_source': {'room_id': '111'}})
        result = await routes['settings_post']({'danmaku_source': {'room_id': '222'}})
        self.assertEqual(result['settings']['danmaku_source']['room_id'], '222')
        source.stop.assert_awaited_once()
        source.start.assert_awaited_once()
        engine.start.assert_not_awaited()
        engine.stop.assert_not_awaited()

    async def test_unrelated_setting_does_not_interrupt_subscription(self):
        routes, engine, source = self.routes({'danmaku_source': {'room_id': '111'}})
        await routes['settings_post']({'stage': {'cap_overlay': True}})
        source.stop.assert_not_awaited()
        source.start.assert_not_awaited()
        engine.stop.assert_not_awaited()

    async def test_reload_changed_room_reconnects_only_source(self):
        routes, engine, source = self.routes({'danmaku_source': {'room_id': '111'}},
                                            {'danmaku_source': {'room_id': '222'}})
        await routes['settings_reload']()
        source.stop.assert_awaited_once()
        source.start.assert_awaited_once()
        engine.start.assert_not_awaited()
        engine.stop.assert_not_awaited()

    async def test_reload_same_room_keeps_subscription(self):
        initial = {'danmaku_source': {'room_id': '111'}}
        routes, _, source = self.routes(initial, initial)
        await routes['settings_reload']()
        source.stop.assert_not_awaited()
        source.start.assert_not_awaited()
