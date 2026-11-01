"""Offline paid-entry identity, source age, streaming and reply-history regressions."""
import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.engine import Engine


class ControlledStream:
    """One complete PCM frame followed by an optional held EOF."""

    def __init__(self, release=None, before_first=None):
        self.release = release
        self.before_first = before_first
        self.waiting = asyncio.Event()
        self.index = 0
        self.close_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index == 0:
            self.index = 1
            if self.before_first:
                self.before_first()
            return bytes(960)  # 20 ms at 24 kHz, mono PCM16.
        self.waiting.set()
        if self.release is not None:
            await self.release.wait()
        raise StopAsyncIteration

    async def aclose(self):
        self.close_count += 1


class DanmakuReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.wall = 1_800_000_000.0
        self.conf = {
            "tts": {"engine": "breezeblue", "chunk_ms": 20, "breezeblue_streaming": True},
            "llm": {"system_prompt": "离线测试人物", "history_turns": 8},
            "danmaku": {"answer_only": True, "max_age_ms": 20000,
                        "batch": {"enabled": True, "max_items": 8, "read_max": 0}},
        }
        self.tts = SimpleNamespace(stream_pcm=Mock(side_effect=lambda *args: ControlledStream()))
        clock = SimpleNamespace(monotonic=lambda: self.now, time=lambda: self.wall,
                                strftime=time.strftime)
        # All configuration is fake. Never instantiate bridge or contact a provider.
        for p in (patch("src.engine.appconf.load", return_value=self.conf),
                  patch("src.engine.appconf.reload", return_value=self.conf),
                  patch("src.engine.ttsmod.create_tts", return_value=self.tts),
                  patch("src.engine.time", clock), patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)
        self.engine = Engine()
        self.engine.phase = "live"
        self.engine.sig = SimpleNamespace(send_audio=AsyncMock(), send_output_text=AsyncMock())
        self.engine._voice_preset = Mock(return_value={"name": "offline-only"})
        self.engine._llm_collect = AsyncMock(return_value=["这是一句离线回答。"])
        # Test admission without allowing an unrelated worker to run.
        self.engine._dan_task = Mock()
        self.engine._dan_task.done.return_value = False

    def enqueue(self, *, event_id="event-1", user_id="user-1", text="你好？", event_ts=0):
        return self.engine.enqueue_danmaku(
            text, content=text, nick="同名观众", platform="测试平台",
            event_id=event_id, user_id=user_id, room_id="offline-room", event_ts=event_ts)

    def item(self, *, event_id="turn-1", user_id="user-1", text="你好？", age=0):
        return {"text": text, "content": text, "nick": "测试观众", "answer_only": True,
                "event_id": event_id, "user_id": user_id, "room_id": "offline-room",
                "platform": "测试平台", "queued_at": self.now - age, "received_at": self.now}

    def test_same_event_id_cannot_enter_paid_queue_twice_even_if_text_changes(self):
        self.assertTrue(self.enqueue()["queued"])
        result = self.enqueue(text="换了一句话？")
        self.assertFalse(result["queued"])
        self.assertEqual(result["reason"], "duplicate")
        self.assertEqual(len(self.engine._dan_buf), 1)

    def test_different_event_ids_same_user_content_share_twenty_second_window(self):
        self.assertTrue(self.enqueue(text="Hello  你好？")["queued"])
        self.assertEqual(self.enqueue(event_id="event-2", text="hello 你好？")["reason"], "duplicate")
        self.now += 19
        self.assertEqual(self.enqueue(event_id="event-3", text="hello 你好？")["reason"], "duplicate")
        self.now += 1
        self.assertTrue(self.enqueue(event_id="event-4", text="hello 你好？")["queued"])
        self.assertEqual(len(self.engine._dan_buf), 2)

    def test_different_user_ids_are_not_merged_despite_same_nickname_and_content(self):
        self.assertTrue(self.enqueue()["queued"])
        self.assertTrue(self.enqueue(event_id="event-2", user_id="user-2")["queued"])
        self.assertEqual([item["user_id"] for item in self.engine._dan_buf], ["user-1", "user-2"])

    def test_original_source_age_is_rejected_or_preserved_in_turn_deadline(self):
        old_ts = int((self.wall - 21) * 1000)
        self.assertEqual(self.enqueue(event_ts=old_ts)["reason"], "stale")
        self.assertEqual(len(self.engine._dan_buf), 0)
        self.assertEqual(len(self.engine._dan_seen_events), 0)
        source_ts = int((self.wall - 15) * 1000)
        self.assertTrue(self.enqueue(event_id="recent", event_ts=source_ts)["queued"])
        item = self.engine._dan_buf[-1]
        self.assertEqual(item["queued_at"], self.now - 15)
        self.assertEqual(item["event_ts"], source_ts)
        turn = self.engine._new_reply_turn(item, "danmaku")
        self.assertEqual(turn["deadline"], self.now + 5)
        self.assertEqual(turn["event_id"], "recent")

    def test_future_source_timestamp_does_not_extend_freshness_deadline(self):
        self.assertTrue(self.enqueue(event_ts=int((self.wall + 30) * 1000))["queued"])
        turn = self.engine._new_reply_turn(self.engine._dan_buf[-1], "danmaku")
        self.assertEqual(turn["queued_at"], self.now)
        self.assertEqual(turn["deadline"], self.now + 20)

    def test_idle_does_not_consume_event_or_content_dedup_window(self):
        self.engine.phase = "idle"
        self.assertEqual(self.enqueue()["reason"], "idle")
        self.assertEqual(len(self.engine._dan_seen_events), 0)
        self.assertEqual(len(self.engine._dan_seen_content), 0)
        self.engine.phase = "live"
        self.assertTrue(self.enqueue()["queued"])

    async def test_stream_first_pcm_expiring_after_tts_start_is_not_sent_or_remembered(self):
        def make_stale():
            self.now += 21
        stream = ControlledStream(before_first=make_stale)
        self.tts.stream_pcm.side_effect = None
        self.tts.stream_pcm.return_value = stream
        await self.engine._speak_danmaku_batch([self.item()])
        self.engine._llm_collect.assert_awaited_once()
        self.assertFalse(self.engine._llm_collect.await_args.kwargs["commit"])
        self.engine.sig.send_audio.assert_not_awaited()
        self.engine.sig.send_output_text.assert_not_awaited()
        self.assertEqual(list(self.engine.llm.history), [])
        self.assertEqual(self.engine._reply_metrics["outcome"], "stale")
        self.assertEqual(stream.close_count, 1)
        self.assertIsNone(self.engine._audio_owner)

    async def test_history_is_committed_only_after_stream_completes_and_subtitle_follows_pcm(self):
        release = asyncio.Event()
        stream = ControlledStream(release)
        self.tts.stream_pcm.side_effect = None
        self.tts.stream_pcm.return_value = stream

        async def subtitle(_):
            self.engine.sig.send_audio.assert_awaited()
        self.engine.sig.send_output_text.side_effect = subtitle
        task = asyncio.create_task(self.engine._speak_danmaku_batch([self.item()]))
        try:
            await asyncio.wait_for(stream.waiting.wait(), 1)
            self.assertEqual(list(self.engine.llm.history), [])
            self.engine.sig.send_output_text.assert_awaited_once()
            release.set()
            await asyncio.wait_for(task, 1)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.engine.llm.history), 2)
        self.assertEqual(self.engine.llm.history[-1]["content"], "这是一句离线回答。")
        self.assertEqual(self.engine._reply_metrics["outcome"], "sent")
        self.assertEqual(self.engine._reply_metrics["sent_bytes"], 960)
        self.assertEqual(stream.close_count, 1)

    async def test_cancelled_partial_stream_does_not_commit_full_answer_or_leave_owner(self):
        release = asyncio.Event()
        stream = ControlledStream(release)
        self.tts.stream_pcm.side_effect = None
        self.tts.stream_pcm.return_value = stream
        task = asyncio.create_task(self.engine._speak_danmaku_batch([self.item()]))
        try:
            await asyncio.wait_for(stream.waiting.wait(), 1)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.engine.sig.send_audio.assert_awaited_once()
        self.assertEqual(list(self.engine.llm.history), [])
        self.assertEqual(self.engine._reply_metrics["outcome"], "cancelled")
        self.assertEqual(stream.close_count, 1)
        self.assertIsNone(self.engine._audio_owner)
        self.assertEqual(self.engine._audio_feeds, 0)

    async def test_identical_spoken_output_is_suppressed_for_forty_five_seconds(self):
        await self.engine._speak_danmaku_batch([self.item()])
        self.assertEqual(self.tts.stream_pcm.call_count, 1)
        self.now += 1
        await self.engine._speak_danmaku_batch([
            self.item(event_id="turn-2", user_id="user-2", text="另一个问题？")])
        self.assertEqual(self.engine._reply_metrics["outcome"], "duplicate_reply")
        self.assertEqual(self.tts.stream_pcm.call_count, 1)
        self.assertEqual(len(self.engine.llm.history), 2)
        self.now += 44
        await self.engine._speak_danmaku_batch([
            self.item(event_id="turn-3", user_id="user-3", text="又一个问题？")])
        self.assertEqual(self.tts.stream_pcm.call_count, 2)
        self.assertEqual(len(self.engine.llm.history), 4)

    async def test_cancellation_only_during_stream_close_is_preserved_after_drain(self):
        closing, release_close = asyncio.Event(), asyncio.Event()

        class HeldCloseStream(ControlledStream):
            async def aclose(inner):
                inner.close_count += 1
                closing.set()
                await release_close.wait()

        stream = HeldCloseStream()
        self.tts.stream_pcm.side_effect = None
        self.tts.stream_pcm.return_value = stream
        task = asyncio.create_task(self.engine._speak_danmaku_batch([self.item()]))
        try:
            await asyncio.wait_for(closing.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())  # Keep draining the already-sent stream.
            release_close.set()
            result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
        finally:
            release_close.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(list(self.engine.llm.history), [])
        self.assertEqual(self.engine._reply_metrics["outcome"], "cancelled")
        self.assertEqual(stream.close_count, 1)
        self.assertIsNone(self.engine._audio_owner)
        self.assertEqual(self.engine._audio_feeds, 0)

    async def test_empty_generation_finishes_as_no_audio_without_paid_tts_or_history(self):
        self.engine._llm_collect.return_value = []
        await self.engine._speak_danmaku_batch([self.item()])
        self.tts.stream_pcm.assert_not_called()
        self.engine.sig.send_audio.assert_not_awaited()
        self.assertEqual(list(self.engine.llm.history), [])
        self.assertEqual(self.engine._reply_metrics["outcome"], "no_audio")
        self.assertIsNone(self.engine._audio_owner)


if __name__ == "__main__":
    unittest.main()
