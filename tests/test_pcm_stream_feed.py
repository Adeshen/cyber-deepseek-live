"""Offline PCM stream pacing, cancellation and first-audio feedback regressions."""
import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch

from src.engine import Engine


REAL_SLEEP = asyncio.sleep
FRAME_BYTES = 4800  # 100 ms of 24 kHz, mono, signed PCM16.


class ChunkStream:
    def __init__(self, chunks, *, before_next=None):
        self.chunks = list(chunks)
        self.index = 0
        self.before_next = before_next
        self.closed = False
        self.close_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        if self.before_next:
            await self.before_next(self.index)
        chunk = self.chunks[self.index]
        self.index += 1
        return chunk

    async def aclose(self):
        self.closed = True
        self.close_count += 1


class PCMStreamFeedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        conf = {"tts": {"engine": "breeze", "chunk_ms": 100}}
        with patch("src.engine.appconf.load", return_value=conf), \
                patch("src.engine.ttsmod.create_tts"):
            self.engine = Engine()
        self.engine.phase = "live"
        self.frames = []
        self.sent_at = []
        self.now = 1000.0
        self.sleeps = []

        async def send_audio(piece):
            self.frames.append(piece)
            self.sent_at.append(self.now)

        async def clock_sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            self.sleeps.append(seconds)
            self.now += seconds
            await REAL_SLEEP(0)

        self.engine.sig = SimpleNamespace(send_audio=AsyncMock(side_effect=send_audio))
        clock = SimpleNamespace(monotonic=lambda: self.now, time=time.time)
        for p in (patch("src.engine.appconf.load", return_value=conf),
                  patch("src.engine.time", clock),
                  patch("src.engine.asyncio.sleep", new=clock_sleep),
                  patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def turn(self, **kwargs):
        return {"sent_bytes": 0, "outcome": "preparing", **kwargs}

    async def test_odd_network_chunks_keep_pcm_samples_frames_and_tail_in_order(self):
        pcm = bytes(range(256)) * 37 + bytes(range(202))  # 9674 B: 200 ms + 74 B.
        self.assertEqual(len(pcm), FRAME_BYTES * 2 + 74)
        cuts = [1, 4799, 4801, 4806, 7807]
        bounds = [0] + cuts + [len(pcm)]
        stream = ChunkStream([pcm[a:b] for a, b in zip(bounds, bounds[1:])])
        turn = self.turn()

        async def first():
            self.assertEqual(len(self.frames), 1)
            self.assertEqual(turn["sent_bytes"], FRAME_BYTES)
            self.assertIn("first_pcm_at", turn)

        on_first = AsyncMock(side_effect=first)
        self.assertTrue(await self.engine._feed_pcm_stream(stream, asyncio.Event(), on_first, turn))
        self.assertEqual([len(x) for x in self.frames], [FRAME_BYTES, FRAME_BYTES, 74])
        self.assertTrue(all(len(x) % 2 == 0 for x in self.frames))
        self.assertEqual(b"".join(self.frames), pcm)
        self.assertEqual(turn["sent_bytes"], len(pcm))
        self.assertEqual(turn["first_pcm_at"], 1000.0)
        on_first.assert_awaited_once_with()
        self.assertEqual(stream.close_count, 1)
        self.assertEqual(self.engine._audio_feeds, 0)

    async def test_frames_use_one_playback_clock_across_network_chunks(self):
        stream = ChunkStream([b"a" * FRAME_BYTES, b"b" * FRAME_BYTES,
                              b"c" * FRAME_BYTES])
        self.assertTrue(await self.engine._feed_pcm_stream(stream, asyncio.Event()))
        self.assertEqual(len(self.sent_at), 3)
        for actual, expected in zip(self.sent_at, [1000.0, 1000.1, 1000.2]):
            self.assertAlmostEqual(actual, expected, places=6)
        self.assertAlmostEqual(self.now, 1000.3, places=6)
        self.assertTrue(stream.closed)

    async def test_slow_network_preserves_original_audio_order(self):
        async def network_wait(index):
            if index == 1:
                self.now += 0.25

        chunks = [b"a" * FRAME_BYTES, b"b" * FRAME_BYTES, b"c" * 120]
        stream = ChunkStream(chunks, before_next=network_wait)
        self.assertTrue(await self.engine._feed_pcm_stream(stream, asyncio.Event()))
        self.assertEqual(self.frames, chunks)
        self.assertAlmostEqual(self.sent_at[1] - self.sent_at[0], 0.35, places=6)
        self.assertTrue(stream.closed)

    async def test_event_stops_cached_audio_after_first_frame_and_closes(self):
        evt = asyncio.Event()
        stream = ChunkStream([b"a" * FRAME_BYTES * 3])
        on_first = AsyncMock(side_effect=evt.set)
        self.assertFalse(await self.engine._feed_pcm_stream(stream, evt, on_first))
        self.assertEqual(len(self.frames), 1)
        on_first.assert_awaited_once()
        self.assertTrue(stream.closed)
        self.assertEqual(self.engine._audio_feeds, 0)

    async def test_event_set_during_network_wait_prevents_first_send(self):
        waiting, release = asyncio.Event(), asyncio.Event()

        async def network_wait(_):
            waiting.set()
            await release.wait()

        stream = ChunkStream([b"a" * FRAME_BYTES], before_next=network_wait)
        evt = asyncio.Event()
        first = AsyncMock()
        task = asyncio.create_task(self.engine._feed_pcm_stream(stream, evt, first))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            evt.set()
            release.set()
            self.assertFalse(await asyncio.wait_for(task, 1))
            self.assertEqual(self.frames, [])
            first.assert_not_awaited()
            self.assertTrue(stream.closed)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_during_network_wait_closes_and_never_sends_tail(self):
        waiting = asyncio.Event()

        async def network_wait(index):
            if index == 1:
                waiting.set()
                await asyncio.Future()

        stream = ChunkStream([b"a" * FRAME_BYTES, b"b" * FRAME_BYTES],
                             before_next=network_wait)
        task = asyncio.create_task(self.engine._feed_pcm_stream(stream, asyncio.Event()))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertEqual(self.frames, [b"a" * FRAME_BYTES])
            self.assertEqual(stream.close_count, 1)
            self.assertEqual(self.engine._audio_feeds, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_send_does_not_claim_first_pcm_or_call_first_callback(self):
        self.engine.sig.send_audio.side_effect = OSError("offline simulated disconnect")
        stream = ChunkStream([b"a" * FRAME_BYTES])
        first = AsyncMock()
        turn = self.turn()
        self.assertFalse(await self.engine._feed_pcm_stream(stream, asyncio.Event(), first, turn))
        self.assertEqual(turn["outcome"], "send_error")
        self.assertEqual(turn["sent_bytes"], 0)
        self.assertNotIn("first_pcm_at", turn)
        self.assertEqual(self.engine._audio_last_feed, 0)
        first.assert_not_awaited()
        self.assertTrue(stream.closed)
        self.assertEqual(self.engine._audio_feeds, 0)

    async def test_turn_expiring_before_first_complete_frame_is_not_sent(self):
        async def network_wait(index):
            if index == 1:
                self.now += 0.3

        stream = ChunkStream([b"a", b"b" * (FRAME_BYTES - 1)],
                             before_next=network_wait)
        first = AsyncMock()
        turn = self.turn(deadline=1000.2)
        before = self.engine._dan_stale
        self.assertFalse(await self.engine._feed_pcm_stream(stream, asyncio.Event(), first, turn))
        self.assertEqual(turn["outcome"], "stale")
        self.assertEqual(self.engine._dan_stale, before + 1)
        self.assertEqual(turn["sent_bytes"], 0)
        self.assertNotIn("first_pcm_at", turn)
        self.assertEqual(self.frames, [])
        first.assert_not_awaited()
        self.assertTrue(stream.closed)

    async def test_preparing_until_first_success_then_sending_and_finally_idle(self):
        stages = []

        async def network_wait(_):
            stages.append(self.engine.response_state()["stage"])

        async def first():
            state = self.engine.response_state()
            stages.append(state["stage"])
            self.assertFalse(state["playback_confirmed"])

        stream = ChunkStream([b"a" * FRAME_BYTES], before_next=network_wait)
        self.assertTrue(await self.engine._own_audio())
        try:
            self.assertTrue(await self.engine._feed_pcm_stream(stream, asyncio.Event(), first))
            self.assertEqual(stages, ["preparing", "sending_audio"])
            self.assertEqual(self.engine.response_state()["stage"], "preparing")
        finally:
            self.engine._release_audio()
        self.assertEqual(self.engine.response_state()["stage"], "idle")

    async def test_incomplete_sample_at_eof_is_rejected_and_stream_closed(self):
        stream = ChunkStream([b"a" * 3])
        with self.assertRaisesRegex(RuntimeError, "PCM"):
            await self.engine._feed_pcm_stream(stream, asyncio.Event())
        self.assertEqual(self.frames, [])
        self.assertTrue(stream.closed)
        self.assertEqual(self.engine._audio_feeds, 0)

    async def test_empty_stream_closes_without_claiming_first_audio(self):
        stream = ChunkStream([])
        first = AsyncMock()
        turn = self.turn()
        self.assertFalse(await self.engine._feed_pcm_stream(stream, asyncio.Event(), first, turn))
        self.assertEqual(turn["sent_bytes"], 0)
        self.assertNotIn("first_pcm_at", turn)
        first.assert_not_awaited()
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
