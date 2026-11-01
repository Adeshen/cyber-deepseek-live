"""Offline regressions for live reply selection, persona and honest feedback."""
import asyncio
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from src.engine import Engine


class VTuberFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The real config can contain keys. Never load it or call a provider.
        self.conf = {
            "tts": {"engine": "breeze"},
            "llm": {"api_key": "offline-test-key", "key_file": "",
                    "base_url": "https://offline.invalid",
                    "system_prompt": "你是成年鲸鱼娘大肥鱼，DeepSeek 的长公主。"},
            "danmaku": {"max_age_ms": 20000, "answer_only": False,
                        "batch": {"read_max": 1}},
        }
        self.tts = SimpleNamespace(synth_pcm=AsyncMock(return_value=b""))
        for name, value in (("src.engine.appconf.load", self.conf),
                            ("src.engine.appconf.reload", self.conf),
                            ("src.engine.ttsmod.create_tts", self.tts)):
            p = patch(name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.engine = Engine()
        self.engine.phase = "live"
        self.engine.sig = SimpleNamespace(send_output_text=AsyncMock(),
                                          send_audio=AsyncMock())
        self.engine._voice_preset = Mock(return_value={"name": "offline"})

    def item(self, nick, content, *, age=0, answer_only=False):
        return {"nick": nick, "text": content, "content": content,
                "answer_only": answer_only,
                "queued_at": time.monotonic() - age}

    @staticmethod
    def context(prompt):
        return json.loads(prompt[prompt.index("{"):prompt.rindex("}") + 1])

    def test_batch_has_one_target_and_nearby_is_context_only(self):
        question = self.item("阿米", "明天怎么早起？")
        nearby = self.item("小蓝", "我刚煮好一锅饭")
        prompt = self.engine._dan_batch_prompt([question, nearby])
        data = self.context(prompt)
        self.assertEqual(data["target"], {"nick": "阿米", "content": "明天怎么早起？"})
        self.assertEqual(data["nearby"], [{"nick": "小蓝", "content": "我刚煮好一锅饭"}])
        self.assertNotIn(data["target"], data["nearby"])
        self.assertIn("不混答其他人", prompt)

    def test_follow_notification_is_selected_and_not_presented_as_viewer_speech(self):
        follow = {**self.item("阿米", "平台新关注", answer_only=True), "event_kind": "follow"}
        question = self.item("小蓝", "今天吃了什么？")
        self.assertIs(self.engine._dan_pick_read([follow, question]), follow)
        prompt = self.engine._dan_batch_prompt([follow, question])
        self.assertIn("不是观众发言", prompt)
        self.assertIn("感谢TA的关注", prompt)
        self.assertNotIn("今天吃了什么", prompt)

    async def test_mixed_batch_does_not_read_silent_action_target(self):
        nearby = self.item("小蓝", "今天太阳不错", answer_only=False)
        action = self.item("阿米", "你能挥手吗？", answer_only=True)
        self.engine._safe_say_text = AsyncMock()
        self.engine._llm_collect = AsyncMock(return_value=[])
        await self.engine._speak_danmaku_batch([nearby, action])
        self.engine._safe_say_text.assert_not_awaited()
        prompt = self.engine._llm_collect.await_args.args[0]
        self.assertEqual(self.context(prompt)["target"]["nick"], "阿米")
        self.assertEqual(self.engine._reply_target, "阿米")
        self.assertIsNone(self.engine._audio_owner)

    async def test_expired_batch_never_acquires_audio_or_calls_llm(self):
        self.engine._own_audio = AsyncMock()
        self.engine._llm_collect = AsyncMock()
        self.engine._safe_say_text = AsyncMock()
        await self.engine._speak_danmaku_batch([
            self.item("旧观众", "刚才发生了什么？", age=21)])
        self.engine._own_audio.assert_not_awaited()
        self.engine._llm_collect.assert_not_awaited()
        self.engine._safe_say_text.assert_not_awaited()
        self.assertEqual(list(self.engine.transcripts), [])

    async def test_expired_question_cannot_steal_fresh_target(self):
        self.conf["danmaku"]["answer_only"] = True
        self.engine._llm_collect = AsyncMock(return_value=[])
        await self.engine._speak_danmaku_batch([
            self.item("旧观众", "为什么不回我？", age=25),
            self.item("新观众", "本公主今天精神不错", age=1)])
        data = self.context(self.engine._llm_collect.await_args.args[0])
        self.assertEqual(data["target"]["nick"], "新观众")
        self.assertEqual(data["nearby"], [])

    async def test_message_expiring_while_waiting_releases_owner_without_reply(self):
        item = self.item("阿米", "刚刚发生了什么？")
        own_audio = self.engine._own_audio

        async def wait_for_previous_speech(**kwargs):
            # Simulate the ownership wait outlasting the comment's 20 s window.
            item["queued_at"] = time.monotonic() - 21
            return await own_audio(**kwargs)

        self.engine._llm_collect = AsyncMock()
        self.engine._safe_say_text = AsyncMock()
        with patch.object(self.engine, "_own_audio", side_effect=wait_for_previous_speech), \
                patch.object(self.engine, "_release_audio", wraps=self.engine._release_audio) as release:
            await self.engine._speak_danmaku_batch([item])
        self.engine._llm_collect.assert_not_awaited()
        self.engine._safe_say_text.assert_not_awaited()
        self.tts.synth_pcm.assert_not_awaited()
        release.assert_called_once()
        self.assertIsNone(self.engine._audio_owner)
        self.assertEqual(list(self.engine.transcripts), [])

    async def test_gift_request_uses_current_character_system_prompt(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {
            "content": "阿米，你这朵花，本公主收下啦。"}}]}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=response)
        with patch("src.engine.httpx.AsyncClient", return_value=client):
            line = await self.engine._gift_llm([
                {"nick": "阿米", "say_nick": "阿米", "item": "玫瑰",
                 "num": 1, "value": 1}], ["阿米"], 0)
        self.assertIn("阿米", line)
        client.post.assert_awaited_once()
        body = client.post.await_args.kwargs["json"]
        system = body["messages"][0]["content"]
        self.assertIn(self.conf["llm"]["system_prompt"], system)
        self.assertIn("礼物反应", system)
        self.assertNotIn("小雅", system)
        self.assertEqual(list(self.engine.llm.history), [])

    async def test_empty_direct_tts_does_not_publish_spoken_text(self):
        self.engine._feed_pcm = AsyncMock()
        await self.engine._safe_say_text("本公主今天很精神。")
        self.tts.synth_pcm.assert_awaited()
        self.engine.sig.send_output_text.assert_not_awaited()
        self.engine._feed_pcm.assert_not_awaited()
        self.assertFalse(any(t["role"] == "dh" for t in self.engine.transcripts))
        self.assertEqual(self.engine.response_state()["stage"], "idle")

    async def test_empty_danmaku_tts_does_not_publish_spoken_reply(self):
        self.conf["danmaku"]["answer_only"] = True
        self.engine._llm_collect = AsyncMock(return_value=["这句回答没有生成音频。"])
        self.engine._feed_pcm = AsyncMock()
        await self.engine._speak_danmaku_batch([self.item("阿米", "你好")])
        self.tts.synth_pcm.assert_awaited()
        self.engine.sig.send_output_text.assert_not_awaited()
        self.engine._feed_pcm.assert_not_awaited()
        self.assertFalse(any(t["role"] == "dh" for t in self.engine.transcripts))

    async def test_empty_chat_tts_does_not_publish_spoken_reply(self):
        async def stream(_, *, commit=True):
            yield "这句回答没有生成音频。"
        self.engine.llm.stream = stream
        self.engine._feed_pcm = AsyncMock()
        await self.engine._speak_llm_task("你好")
        self.tts.synth_pcm.assert_awaited()
        self.engine.sig.send_output_text.assert_not_awaited()
        self.engine._feed_pcm.assert_not_awaited()
        self.assertFalse(any(t["role"] == "dh" for t in self.engine.transcripts))

    async def test_response_state_tracks_preparation_and_real_pcm_send(self):
        self.assertEqual(self.engine.response_state()["stage"], "idle")
        self.assertTrue(await self.engine._own_audio())
        try:
            self.assertEqual(self.engine.response_state()["stage"], "preparing")

            async def send(*_):
                state = self.engine.response_state()
                self.assertEqual(state["stage"], "sending_audio")
                self.assertFalse(state["playback_confirmed"])
                return True

            with patch.object(self.engine, "_feed_pcm_inner", side_effect=send):
                self.assertTrue(await self.engine._feed_pcm(bytes(48), asyncio.Event()))
            self.assertEqual(self.engine.response_state()["stage"], "preparing")
        finally:
            self.engine._release_audio()
        self.assertEqual(self.engine.response_state()["stage"], "idle")
        self.engine._dan_buf.append(self.item("阿米", "你好"))
        state = self.engine.response_state()
        self.assertEqual(state["stage"], "queued")
        self.assertEqual(state["pending"], 1)
        self.engine.phase = "idle"
        self.assertEqual(self.engine.response_state()["stage"], "idle")


if __name__ == "__main__":
    unittest.main()
