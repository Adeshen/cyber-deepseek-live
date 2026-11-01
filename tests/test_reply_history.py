"""Offline regressions: generated text is not a record of sent audio."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.engine import Engine
from src.llm import LLMClient


class ReplyHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conf = {"tts": {"engine": "breeze"},
                     "llm": {"system_prompt": "你是大肥鱼。", "history_turns": 2},
                     "session": {"send_output_per_sentence": True}}
        self.tts = SimpleNamespace(synth_pcm=AsyncMock(return_value=bytes(48)))
        for name, value in (("src.engine.appconf.load", self.conf),
                            ("src.engine.ttsmod.create_tts", self.tts)):
            p = patch(name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.engine = Engine()
        self.engine.phase = "live"
        self.engine.sig = SimpleNamespace(send_output_text=AsyncMock())
        self.engine._voice_preset = Mock(return_value={})
        self.engine._merge_chars = lambda: (8, 6)

    def use_stream(self, *segments):
        async def stream(text, *, commit=True):
            self.assertFalse(commit, "Audio generation must defer history commit")
            for segment in segments:
                yield segment
        self.engine.llm.stream = stream

    async def consume_provider_stream(self, client, *, commit=None):
        # Exercise the real LLM HTTP/SSE implementation using only an in-memory transport.
        data = json.dumps({"choices": [{"delta": {"content": "离线回答。"}}]}, ensure_ascii=False)
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=f"data: {data}\n\ndata: [DONE]\n\n".encode()))
        real_client = httpx.AsyncClient
        client.api_key = lambda: "offline-test-key"
        with patch("src.llm.httpx.AsyncClient", side_effect=lambda **kwargs:
                   real_client(transport=transport, **kwargs)):
            kwargs = {} if commit is None else {"commit": commit}
            return "".join([piece async for piece in client.stream("你好", **kwargs)])

    async def test_stream_default_preserves_automatic_history_and_false_defers_it(self):
        client = LLMClient(lambda: {"base_url": "https://offline.invalid", "history_turns": 1})
        self.assertEqual(await self.consume_provider_stream(client, commit=False), "离线回答。")
        self.assertEqual(list(client.history), [])
        self.assertEqual(await self.consume_provider_stream(client), "离线回答。")
        self.assertEqual(list(client.history), [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "离线回答。"}])

    def test_explicit_commit_trims_history_and_ignores_empty_output(self):
        client = LLMClient(lambda: {"history_turns": 1})
        client.commit_exchange("旧问题", "旧回答。")
        client.commit_exchange("新问题", "已完整发送。")
        client.commit_exchange("未播问题", "  ")
        self.assertEqual(list(client.history), [
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "已完整发送。"}])

    async def test_collect_can_defer_commit_without_changing_default(self):
        calls = []

        async def stream(text, *, commit=True):
            calls.append((text, commit))
            yield "第一句。"

        self.engine.llm.stream = stream
        self.assertEqual(await self.engine._llm_collect("默认"), ["第一句。"])
        self.assertEqual(await self.engine._llm_collect("延后", commit=False), ["第一句。"])
        self.assertEqual(calls, [("默认", True), ("延后", False)])

    async def test_only_complete_sent_segments_enter_history_in_both_modes(self):
        for per_sentence in (True, False):
            with self.subTest(per_sentence=per_sentence):
                self.conf["session"]["send_output_per_sentence"] = per_sentence
                self.engine.llm.reset_history()
                self.use_stream("第一句已发出。", "第二句未发出。")
                self.engine._feed_pcm = AsyncMock(side_effect=[True, False])
                await self.engine._speak_llm_task("本轮问题")
                self.assertEqual(list(self.engine.llm.history), [
                    {"role": "user", "content": "本轮问题"},
                    {"role": "assistant", "content": "第一句已发出。"}])
                self.assertIsNone(self.engine._audio_owner)

    async def test_empty_or_unsent_audio_never_enters_history(self):
        for pcm in (b"", bytes(48)):
            with self.subTest(has_audio=bool(pcm)):
                self.engine.llm.reset_history()
                self.tts.synth_pcm.return_value = pcm
                self.use_stream("这句没有成功发出。")
                self.engine._feed_pcm = AsyncMock(return_value=False)
                await self.engine._speak_llm_task("本轮问题")
                self.assertEqual(list(self.engine.llm.history), [])

    async def test_cancelled_reply_remembers_only_the_previous_complete_segment(self):
        sending_tail = asyncio.Event()
        sent_count = 0

        async def feed(*_):
            nonlocal sent_count
            sent_count += 1
            if sent_count == 1:
                return True
            sending_tail.set()
            await asyncio.Future()

        self.use_stream("第一句已发出。", "第二句尚未发完。")
        self.engine._feed_pcm = feed
        task = asyncio.create_task(self.engine._speak_llm_task("取消中途的回复"))
        try:
            await asyncio.wait_for(sending_tail.wait(), 1)
            self.assertEqual(list(self.engine.llm.history), [])
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertEqual(list(self.engine.llm.history), [
                {"role": "user", "content": "取消中途的回复"},
                {"role": "assistant", "content": "第一句已发出。"}])
            self.assertIsNone(self.engine._audio_owner)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_full_queue_producer_is_drained(self):
        feed_started = asyncio.Event()
        queue_blocked = asyncio.Event()
        real_queue = asyncio.Queue

        class TrackedQueue(real_queue):
            async def put(inner, item):
                if inner.full():
                    queue_blocked.set()
                return await super().put(item)

        async def feed(*_):
            feed_started.set()
            await asyncio.Future()

        self.engine._feed_pcm = feed
        self.use_stream(*[f"第{i}段不会完整发送。" for i in range(30)])
        real_create = asyncio.create_task
        producers = []

        def create(coro):
            task = real_create(coro)
            producers.append(task)
            return task

        consumer = None
        try:
            with patch("src.engine.asyncio.Queue", TrackedQueue), \
                    patch("src.engine.asyncio.create_task", side_effect=create):
                consumer = real_create(self.engine._speak_llm_task("队列满时取消"))
                await asyncio.wait_for(feed_started.wait(), 1)
                await asyncio.wait_for(queue_blocked.wait(), 1)
                consumer.cancel()
                done, _ = await asyncio.wait({consumer}, timeout=0.3)
                self.assertIn(consumer, done, "Producer cleanup blocked on full queue")
                await asyncio.gather(consumer, return_exceptions=True)
                self.assertTrue(producers)
                self.assertTrue(all(task.done() for task in producers))
            self.assertEqual(list(self.engine.llm.history), [])
            self.assertIsNone(self.engine._audio_owner)
        finally:
            for task in ([consumer] if consumer else []) + producers:
                task.cancel()
            await asyncio.gather(*([consumer] if consumer else []), *producers,
                                 return_exceptions=True)

    async def test_cancel_waits_for_inflight_synthesis_cleanup(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        drained = asyncio.Event()

        async def synth(*_):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await drained.wait()  # Mimic provider cancellation drain, with no network.
                raise

        self.tts.synth_pcm = synth
        self.use_stream("尚未返回的合成音频。")
        consumer = asyncio.create_task(self.engine._speak_llm_task("取消首句"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            consumer.cancel()
            await asyncio.wait_for(cancelled.wait(), 1)
            self.assertFalse(consumer.done())
            drained.set()
            result = await asyncio.gather(consumer, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertEqual(list(self.engine.llm.history), [])
            self.engine.sig.send_output_text.assert_not_awaited()
            self.assertIsNone(self.engine._audio_owner)
        finally:
            drained.set()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
