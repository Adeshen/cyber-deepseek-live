"""Offline regression: action acceptance/queueing must not masquerade as sending."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src import dm_router
from src.engine import Engine


class ActionFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def pair(self):
        # Do not import bridge (its globals load private config), read any key,
        # or create provider connections. Only the actual local action code runs.
        with patch("src.engine.appconf.load", return_value={"tts": {"engine": "breeze"}}), \
                patch("src.engine.ttsmod.create_tts"):
            engine = Engine()
        router = dm_router.DanmakuRouter()
        router.engine = engine
        router._conf_get = lambda: {"cooldown_s": 0, "same_action_windows": [0, 0, 0]}
        router.blocked = lambda *args: None
        router._redlined = lambda text: None
        router.canonical = lambda text: text.strip()
        engine.phase = "live"
        engine.sig = SimpleNamespace(send_input_text=AsyncMock())
        engine._ws_ok = lambda: True
        engine._blocked_cmd = lambda *args: None
        engine._log_exc = Mock()
        engine.on_action_fire = router.note_manual  # Existing bridge binding, with source.
        engine._act_cfg = lambda: {"qmax": 1, "same": 999}
        engine._act_gap_for = lambda text, source: 0
        # Let each test drain the actual queue explicitly instead of spawning timers.
        engine._act_task = Mock()
        engine._act_task.done.return_value = False
        return engine, router

    def defer(self, engine):
        engine._act_last = asyncio.get_running_loop().time()
        engine._act_gap_for = lambda text, source: 999

    async def drain(self, engine):
        engine._act_gap_for = lambda text, source: 0
        await engine._act_drain()

    def fires(self, router):
        return [item for item in router.cmd_feed()["recent"] if item["kind"] == "fire"]

    def total(self, router):
        return sum(item["count"] for item in router.top())

    def manual_endpoint(self, engine, router):
        # Execute the real route body without executing module-level startup/config.
        path = Path(__file__).resolve().parents[1] / "src" / "bridge.py"
        module = ast.parse(path.read_text(encoding="utf-8"))
        handler = next(node for node in module.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "vidu_cmd")
        handler.decorator_list = []
        code = compile(ast.Module(body=[handler], type_ignores=[]), str(path), "exec")
        namespace = {"engine": engine, "dm_router": router, "CmdBody": SimpleNamespace}
        exec(code, namespace)
        return namespace["vidu_cmd"]

    async def test_immediate_auto_and_confirm_are_counted_once_after_send(self):
        for source in ("t0", "t0c", "t0p", "t2", "trust", "confirm", "auto"):
            with self.subTest(source=source):
                engine, router = self.pair()
                self.assertTrue(await router.do_fire("比心", source))
                engine.sig.send_input_text.assert_awaited_once_with("比心")
                self.assertEqual(self.total(router), 1)
                self.assertEqual(router._fire_count, {"比心": 1})
                self.assertEqual(len(self.fires(router)), 1)
                self.assertEqual(self.fires(router)[0]["source"], source)

    async def test_deferred_action_is_only_counted_when_queue_really_sends(self):
        engine, router = self.pair()
        self.defer(engine)
        self.assertTrue(await router.do_fire("比心", "t0"))
        engine.sig.send_input_text.assert_not_awaited()
        self.assertEqual(self.total(router), 0)
        self.assertEqual(router._fire_hist, {})
        self.assertEqual(self.fires(router), [])
        self.assertEqual(router.cmd_feed()["last"], {})
        self.assertEqual(router.cmd_feed()["recent"][-1]["kind"], "queued")
        await self.drain(engine)
        engine.sig.send_input_text.assert_awaited_once_with("比心")
        self.assertEqual(self.total(router), 1)
        self.assertEqual(len(self.fires(router)), 1)

    async def test_queue_overflow_does_not_count_dropped_action(self):
        engine, router = self.pair()
        self.defer(engine)
        await router.do_fire("比心", "t0")
        await router.do_fire("挥手", "t0")
        self.assertEqual(engine._act_dropped, 1)
        self.assertEqual(self.total(router), 0)
        await self.drain(engine)
        engine.sig.send_input_text.assert_awaited_once_with("挥手")
        self.assertEqual([item["text"] for item in self.fires(router)], ["挥手"])
        self.assertEqual(router._fire_count, {"挥手": 1})

    async def test_queue_dedup_does_not_double_count_or_double_fire(self):
        engine, router = self.pair()
        self.defer(engine)
        await router.do_fire("比心", "t0")
        await router.do_fire("比心", "t0")
        self.assertEqual(len(engine._act_q), 1)
        await self.drain(engine)
        engine.sig.send_input_text.assert_awaited_once_with("比心")
        self.assertEqual(self.total(router), 1)
        self.assertEqual(len(self.fires(router)), 1)

    async def test_disconnected_or_newly_blocked_queue_never_reports_fire(self):
        for reason in ("disconnected", "blocked"):
            with self.subTest(reason=reason):
                engine, router = self.pair()
                self.defer(engine)
                await router.do_fire("比心", "t0")
                if reason == "disconnected":
                    engine._ws_ok = lambda: False
                else:
                    engine._blocked_cmd = lambda *args: "offline blocked action"
                await self.drain(engine)
                engine.sig.send_input_text.assert_not_awaited()
                self.assertEqual(self.total(router), 0)
                self.assertEqual(self.fires(router), [])

    async def test_transport_failure_never_counts_or_reports_fire(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                engine, router = self.pair()
                engine.sig.send_input_text.side_effect = OSError("offline simulated failure")
                if deferred:
                    self.defer(engine)
                accepted = await router.do_fire("比心", "t0")
                self.assertEqual(accepted, deferred)
                if deferred:
                    await self.drain(engine)
                self.assertEqual(self.total(router), 0)
                self.assertEqual(self.fires(router), [])
                self.assertEqual(router._fire_count, {})

    async def test_manual_route_keeps_one_top10_count_without_auto_cooldown(self):
        engine, router = self.pair()
        endpoint = self.manual_endpoint(engine, router)
        result = await endpoint(SimpleNamespace(text="比心", silent=False))
        self.assertFalse(result["deferred"])
        self.assertEqual(self.total(router), 1)
        self.assertEqual(router._last_fire, 0)
        self.assertEqual(router._fire_hist, {})
        self.assertEqual(router._fire_count, {})
        self.assertEqual(self.fires(router), [])  # Manual actions stay out of audience feed.

    async def test_manual_route_does_not_count_queued_action_that_is_dropped(self):
        engine, router = self.pair()
        self.defer(engine)
        endpoint = self.manual_endpoint(engine, router)
        self.assertTrue((await endpoint(SimpleNamespace(text="比心", silent=False)))["deferred"])
        self.assertEqual(self.total(router), 0)
        engine._ws_ok = lambda: False
        await self.drain(engine)
        self.assertEqual(self.total(router), 0)
        self.assertEqual(self.fires(router), [])

    async def test_random_action_neither_calls_callback_nor_changes_stats(self):
        engine, router = self.pair()
        callback = Mock(wraps=router.note_manual)
        engine.on_action_fire = callback
        await engine.cmd("比心", source="random")
        engine.sig.send_input_text.assert_awaited_once_with("比心")
        callback.assert_not_called()
        self.assertEqual(self.total(router), 0)
        self.assertEqual(router._last_fired_text, "")

    async def test_existing_module_callback_forwards_source(self):
        engine, router = self.pair()
        engine.on_action_fire = dm_router.note_manual
        with patch.object(dm_router, "router", router):
            await router.do_fire("比心", "confirm")
        self.assertEqual(self.total(router), 1)
        self.assertEqual(self.fires(router)[0]["source"], "confirm")


if __name__ == "__main__":
    unittest.main()
