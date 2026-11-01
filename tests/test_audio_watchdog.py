"""Offline regression: first TTS synthesis must not look like an ancient stall."""
import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.engine import Engine


class AudioWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Never load the real config, create a session, or contact either provider.
        with patch("src.engine.appconf.load", return_value={"tts": {"engine": "breeze"}}), \
                patch("src.engine.ttsmod.create_tts"):
            self.engine = Engine()
        self.engine.phase = "live"
        self.engine.sig = SimpleNamespace(interrupt=AsyncMock(), send_audio=AsyncMock())
        self.engine._dan_buf.append({"text": "offline pending event"})
        self.engine._dan_task = Mock()
        self.engine._dan_task.done.return_value = False
        self.now = 1000.0
        self.clock = SimpleNamespace(monotonic=lambda: self.now,
                                     time=lambda: 1_793_352_531.0,
                                     strftime=time.strftime)

    async def acquire_background_owner(self):
        acquired = asyncio.Event()

        async def synthesize_first_sentence():
            await self.engine._own_audio()
            acquired.set()
            try:
                await asyncio.Future()
            finally:
                self.engine._release_audio()

        task = asyncio.create_task(synthesize_first_sentence())
        self.addAsyncCleanup(self.cancel_owner, task)
        await acquired.wait()
        return task

    async def cancel_owner(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def watchdog_tick(self):
        # Run the actual watchdog body once, without a real five-second wait.
        calls = 0

        async def tick_sleep(seconds):
            nonlocal calls
            self.assertEqual(seconds, 5)
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError

        with patch("src.engine.asyncio.sleep", new=tick_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.engine._dan_watchdog_loop()

    async def test_first_tts_is_not_cancelled_before_first_pcm(self):
        with patch("src.engine.time", self.clock):
            owner = await self.acquire_background_owner()
            self.assertEqual(self.engine._audio_last_feed, 0)
            self.now += 3
            await self.watchdog_tick()
            self.assertEqual(owner.cancelling(), 0)
            self.assertIs(self.engine._audio_owner, owner)
            self.engine.sig.interrupt.assert_not_awaited()

    async def test_new_owner_does_not_inherit_previous_owners_old_feed(self):
        self.engine._audio_last_feed = 10.0
        self.engine._audio_acquired_at = 5.0
        with patch("src.engine.time", self.clock):
            owner = await self.acquire_background_owner()
            self.assertEqual(self.engine._audio_acquired_at, 1000)
            self.assertEqual(self.engine._audio_last_feed, 0)
            self.now += 39
            await self.watchdog_tick()
            self.assertEqual(owner.cancelling(), 0)
            self.engine.sig.interrupt.assert_not_awaited()

    async def test_same_owner_reentry_does_not_extend_stall_deadline(self):
        with patch("src.engine.time", self.clock):
            await self.engine._own_audio()
            try:
                self.now += 30
                await self.engine._own_audio()
                self.assertEqual(self.engine._audio_acquired_at, 1000)
            finally:
                self.engine._release_audio()
        self.assertIsNone(self.engine._audio_owner)
        self.assertEqual(self.engine._audio_acquired_at, 0)
        self.assertEqual(self.engine._audio_last_feed, 0)

    async def test_recent_pcm_preserves_owner_but_real_stall_is_cancelled(self):
        with patch("src.engine.time", self.clock):
            owner = await self.acquire_background_owner()
            self.engine._audio_last_feed = 1040.0
            self.now = 1060.0
            await self.watchdog_tick()
            self.assertEqual(owner.cancelling(), 0)
            self.now = 1081.0
            with patch("builtins.print"):
                await self.watchdog_tick()
            self.assertEqual(owner.cancelling(), 1)
            self.engine.sig.interrupt.assert_awaited_once()
            self.assertIsNone(self.engine._audio_owner)

    async def test_successful_pcm_send_uses_same_monotonic_clock(self):
        pcm = bytes(48)  # One millisecond of 24 kHz mono PCM16.
        self.engine._ws_ok = lambda: True
        with patch("src.engine.time", self.clock), \
                patch("src.engine.appconf.load", return_value={"tts": {"chunk_ms": 100}}):
            self.now = 1011.0
            self.assertTrue(await self.engine._feed_pcm(pcm, asyncio.Event()))
        self.engine.sig.send_audio.assert_awaited_once_with(pcm)
        self.assertEqual(self.engine._audio_last_feed, 1011.0)

    async def test_failed_pcm_send_does_not_claim_a_successful_feed(self):
        self.engine._audio_last_feed = 1002.0
        self.engine._ws_ok = lambda: True
        self.engine._trace = Mock()
        self.engine.sig.send_audio.side_effect = OSError("offline simulated failure")
        with patch("src.engine.time", self.clock), \
                patch("src.engine.appconf.load", return_value={"tts": {"chunk_ms": 100}}):
            self.now = 1011.0
            self.assertFalse(await self.engine._feed_pcm(bytes(48), asyncio.Event()))
        self.assertEqual(self.engine._audio_last_feed, 1002.0)

    async def test_ready_gift_gets_next_audio_slot_before_normal_danmaku(self):
        self.engine._gift_waiting_for_audio = True
        self.engine._gift_task = Mock()
        waits = []

        async def yield_to_gift(seconds):
            waits.append(seconds)
            self.engine._gift_waiting_for_audio = False

        with patch('src.engine.asyncio.sleep', new=yield_to_gift):
            self.assertTrue(await self.engine._own_audio(preempt=False, wait_only=True))
        self.assertEqual(waits, [0.05])
        self.engine._release_audio()


if __name__ == "__main__":
    unittest.main()
