"""Offline tests: normalized messages and an owned, cancellable fake socket."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.danmaku_source import DanmakuSource


ROOM = "566705425536"


def event(kind="chat", event_id="one", **overrides):
    value = {"id": event_id, "roomId": ROOM, "type": kind,
             "user": {"id": "user-1", "nickname": "观众甲"},
             "data": {"content": "你好"}}
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


class FakeSocket:
    def __init__(self, messages=(), block=True):
        self.messages = list(messages)
        self.block = block
        self.close_calls = 0
        self.waiting = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.pop(0)
        if not self.block:
            raise StopAsyncIteration
        self.waiting.set()
        await asyncio.Future()

    async def close(self):
        self.close_calls += 1


class DanmakuSourceTests(unittest.IsolatedAsyncioTestCase):
    async def source(self, callback=None, **settings):
        conf = {"enabled": False, "url": "ws://127.0.0.1:8757/ws",
                "room_id": ROOM, "platform": "抖音", **settings}
        callback = callback if callback is not None else AsyncMock(return_value={"queued": True})
        source = DanmakuSource(lambda: conf, callback)
        await source.start()
        self.addAsyncCleanup(source.stop)
        return source, callback

    async def test_controls_keep_transport_and_room_status_separate(self):
        socket = FakeSocket([
            event("__hello"), event("__connecting"),
            event("__connected", room={"status": "closed"}),
        ])
        with patch("src.danmaku_source.connect", return_value=socket):
            source, callback = await self.source(enabled=True)
            await asyncio.wait_for(socket.waiting.wait(), 1)
            status = source.status()
            self.assertEqual(status["state"], "connected")
            self.assertEqual(status["room_status"], "closed")
            self.assertEqual(status["counts"]["control"], 3)
            callback.assert_not_awaited()
            await source._handle_message(event("__error", error="secret-url-and-message"))
            self.assertEqual(source.status()["room_status"], "error")
            self.assertNotIn("secret-url", json.dumps(source.status()))
            self.assertEqual(source.status()["counts"]["source_errors"], 1)

    async def test_chat_room_filter_id_dedup_and_bridge_payload(self):
        source, callback = await self.source()
        await source._handle_message(event("__connected", room={"status": "closed"}))
        await source._handle_message(event(roomId="another-room"))
        self.assertEqual(source.status()["room_status"], "closed")
        await source._handle_message(event())
        self.assertEqual(source.status()["room_status"], "live")
        await source._handle_message(event())
        callback.assert_awaited_once_with({"text": "你好", "content": "你好", "nick": "观众甲",
                                          "platform": "抖音", "kind": "", "event_id": "one",
                                          "user_id": "user-1", "room_id": ROOM, "event_ts": 0})
        counts = source.status()["counts"]
        self.assertEqual(counts["wrong_room"], 1)
        self.assertEqual(counts["duplicates"], 1)
        self.assertEqual(counts["queued"], 1)
        self.assertNotIn("你好", json.dumps(source.status(), ensure_ascii=False))

    async def test_gift_final_group_dedup_and_only_incremental_quantity(self):
        source, callback = await self.source()
        data = {"giftId": "gift-1", "giftName": "小星星", "diamondCount": 3,
                "repeatCount": 2, "comboCount": 1, "groupCount": 1,
                "combo": True, "repeatEnd": False, "groupId": "group-1"}
        await source._handle_message(event("gift", "progress", data=data))
        callback.assert_not_awaited()
        self.assertEqual(source.status()["room_status"], "live")
        final = {**data, "repeatEnd": True}
        await source._handle_message(event("gift", "final-one", data=final))
        await source._handle_message(event("gift", "final-one", data=final))
        await source._handle_message(event("gift", "final-alias", data=final))
        await source._handle_message(event("gift", "final-increase", data={**final, "repeatCount": 5}))
        self.assertEqual(callback.await_count, 2)
        first, second = [call.args[0] for call in callback.await_args_list]
        self.assertEqual((first["gift_num"], first["gift_value"]), (2, 6))
        self.assertEqual((second["gift_num"], second["gift_value"]), (3, 9))
        self.assertEqual(first["kind"], "gift")
        self.assertEqual(first["text"], "小星星")
        self.assertEqual(source.status()["counts"]["gift_progress"], 1)
        self.assertEqual(source.status()["counts"]["gift_duplicates"], 1)

    async def test_follow_enters_priority_notification_queue_with_original_identity(self):
        source, callback = await self.source()
        await source._handle_message(event("follow", "follow-1", data={"action": 1}))
        payload = callback.await_args.args[0]
        self.assertEqual(payload["kind"], "follow")
        self.assertEqual(payload["priority"], "vip")
        self.assertTrue(payload["answer_only"])
        self.assertEqual(payload["event_id"], "follow-1")
        self.assertEqual(payload["user_id"], "user-1")
        self.assertEqual(payload["room_id"], ROOM)
        self.assertEqual(source.status()["counts"]["follow"], 1)
        self.assertEqual(source.status()["counts"]["gift"], 0)
        self.assertNotIn("观众甲", json.dumps(source.status(), ensure_ascii=False))

    async def test_unfollow_share_and_unknown_actions_never_trigger_follow_thanks(self):
        source, callback = await self.source()
        for action in [0, 2, 3, True, None]:
            await source._handle_message(event("follow", "social-" + str(action), data={"action": action}))
        callback.assert_not_awaited()
        self.assertEqual(source.status()["counts"]["social_ignored"], 5)

    async def test_follow_event_and_user_dedup_preserve_other_viewers(self):
        source, callback = await self.source()
        await source._handle_message(event("follow", "follow-1", data={"action": 1}))
        await source._handle_message(event("follow", "follow-1", data={"action": 1}))
        await source._handle_message(event("follow", "follow-2", data={"action": 1}))
        await source._handle_message(event("follow", "follow-3", data={"action": 1},
                                          user={"id": "other-user", "nickname": "观众甲"}))
        self.assertEqual(callback.await_count, 2)
        self.assertEqual(source.status()["counts"]["duplicates"], 1)
        self.assertEqual(source.status()["counts"]["follow_duplicates"], 1)

    async def test_idle_follow_is_not_replayed_and_does_not_reserve_user_window(self):
        source, callback = await self.source()
        callback.return_value = {"queued": False, "reason": "idle"}
        await source._handle_message(event("follow", "idle-follow", data={"action": 1}))
        callback.return_value = {"queued": True}
        await source._handle_message(event("follow", "later-follow", data={"action": 1}))
        self.assertEqual(callback.await_count, 2)
        self.assertEqual(source.status()["counts"]["idle"], 1)
        self.assertEqual(source.status()["counts"]["queued"], 1)

    async def test_stale_follow_does_not_enter_speech_queue(self):
        source, callback = await self.source()
        await source._handle_message(event("follow", "old-follow", data={"action": 1}, ts=1))
        callback.assert_not_awaited()
        self.assertEqual(source.status()["counts"]["stale"], 1)

    async def test_different_ids_same_user_normalized_content_share_twenty_second_window(self):
        source, callback = await self.source()
        clock = [1000.0]
        with patch("src.danmaku_source.time", SimpleNamespace(monotonic=lambda: clock[0])):
            await source._handle_message(event(event_id="first", data={"content": "ＡＢＣ  你好？"}))
            await source._handle_message(event(event_id="same-text", data={"content": "abc 你好?"}))
            clock[0] += 19
            await source._handle_message(event(event_id="still-recent", data={"content": "abc 你好?"}))
            self.assertEqual(callback.await_count, 1)
            clock[0] += 1
            await source._handle_message(event(event_id="window-ended", data={"content": "abc 你好?"}))
        self.assertEqual(callback.await_count, 2)
        self.assertEqual(source.status()["counts"]["content_duplicates"], 2)
        self.assertNotIn("你好", json.dumps(source.status(), ensure_ascii=False))

    async def test_different_users_same_content_are_not_merged(self):
        source, callback = await self.source()
        await source._handle_message(event(event_id="user-a"))
        await source._handle_message(event(event_id="user-b", user={"id": "user-2", "nickname": "观众甲"}))
        self.assertEqual(callback.await_count, 2)
        self.assertEqual(source.status()["counts"]["content_duplicates"], 0)
        self.assertEqual(callback.await_args_list[1].args[0]["user_id"], "user-2")

    async def test_missing_user_id_uses_nickname_for_content_window(self):
        source, callback = await self.source()
        await source._handle_message(event(event_id="nick-first", user={"nickname": "观众甲"}))
        await source._handle_message(event(event_id="nick-again", user={"nickname": "观众甲"}))
        await source._handle_message(event(event_id="nick-other", user={"nickname": "观众乙"}))
        self.assertEqual(callback.await_count, 2)
        self.assertEqual(source.status()["counts"]["content_duplicates"], 1)

    async def test_source_time_rejects_old_chat_and_preserves_future_timestamp(self):
        source, callback = await self.source()
        with patch("src.danmaku_source.time.time", return_value=1000.0):
            await source._handle_message(event(event_id="old", ts=979999))
            await source._handle_message(event(event_id="future", ts=1020000))
        callback.assert_awaited_once()
        payload = callback.await_args.args[0]
        self.assertEqual(payload["event_id"], "future")
        self.assertEqual(payload["event_ts"], 1020000)
        self.assertEqual(source.status()["counts"]["stale"], 1)

    async def test_configured_source_max_age_applies_only_to_chat(self):
        source, callback = await self.source(max_age_ms=5000)
        with patch("src.danmaku_source.time.time", return_value=1000.0):
            await source._handle_message(event(event_id="old-chat", ts=994000))
            await source._handle_message(event("gift", "old-gift", ts=900000,
                                              data={"giftId": "g", "giftName": "花", "diamondCount": 1}))
        callback.assert_awaited_once()
        self.assertEqual(callback.await_args.args[0]["kind"], "gift")
        self.assertEqual(callback.await_args.args[0]["event_ts"], 900000)
        self.assertEqual(source.status()["counts"]["stale"], 1)

    async def test_idle_error_and_rejection_do_not_reserve_content_window(self):
        callback = AsyncMock(side_effect=[{"queued": False, "reason": "idle"},
                                          RuntimeError("offline private failure"),
                                          {"queued": False, "reason": "filtered"},
                                          {"queued": True}])
        source, callback = await self.source(callback)
        for event_id in ("idle", "error", "filtered", "queued", "duplicate"):
            await source._handle_message(event(event_id=event_id))
        self.assertEqual(callback.await_count, 4)
        counts = source.status()["counts"]
        self.assertEqual(counts["idle"], 1)
        self.assertEqual(counts["dispatch_errors"], 1)
        self.assertEqual(counts["not_queued"], 1)
        self.assertEqual(counts["queued"], 1)
        self.assertEqual(counts["content_duplicates"], 1)
        self.assertNotIn("private failure", json.dumps(source.status()))

    async def test_content_cache_is_bounded(self):
        source, callback = await self.source()
        with patch("src.danmaku_source._CACHE_LIMIT", 2):
            for i in range(3):
                await source._handle_message(event(event_id=str(i), data={"content": f"消息{i}"}))
        self.assertEqual(callback.await_count, 3)
        self.assertEqual(len(source._content_seen), 2)

    async def test_independent_gift_groups_and_noncombo_gifts(self):
        source, callback = await self.source()
        data = {"giftId": "g", "giftName": "花", "diamondCount": 1, "combo": False,
                "repeatCount": 1, "groupId": "group-a"}
        await source._handle_message(event("gift", "a", data=data))
        await source._handle_message(event("gift", "b", data={**data, "groupId": "group-b"}))
        self.assertEqual(callback.await_count, 2)

    async def test_idle_rejection_and_dispatch_error_are_not_retried(self):
        callback = AsyncMock(side_effect=[{"queued": False, "reason": "idle"},
                                          RuntimeError("do-not-print-raw-url-or-event")])
        source, callback = await self.source(callback)
        await source._handle_message(event(event_id="idle"))
        await source._handle_message(event(event_id="failed"))
        await source._handle_message(event(event_id="failed"))
        self.assertEqual(callback.await_count, 2)
        status = source.status()
        self.assertEqual(status["counts"]["idle"], 1)
        self.assertEqual(status["counts"]["dispatch_errors"], 1)
        self.assertEqual(status["counts"]["dispatched"], 1)
        self.assertNotIn("do-not-print", json.dumps(status))

    async def test_dedup_caches_are_bounded(self):
        source, callback = await self.source()
        with patch("src.danmaku_source._CACHE_LIMIT", 2):
            for i in range(3):
                await source._handle_message(event("gift", str(i), data={
                    "giftId": "g", "giftName": "花", "groupId": str(i), "diamondCount": 1}))
        self.assertEqual(len(source._seen), 2)
        self.assertEqual(len(source._gift_totals), 2)

    async def test_start_is_idempotent_stop_closes_socket_and_cancels_reader(self):
        socket = FakeSocket()
        with patch("src.danmaku_source.connect", return_value=socket) as connection:
            source, _ = await self.source(enabled=True)
            await asyncio.wait_for(socket.waiting.wait(), 1)
            task = source._task
            await source.start()
            self.assertIs(source._task, task)
            self.assertEqual(connection.call_count, 1)
            self.assertIsNone(connection.call_args.kwargs["proxy"])
            await source.stop()
            self.assertTrue(task.done())
            self.assertGreaterEqual(socket.close_calls, 1)
            self.assertEqual(source.status()["state"], "stopped")
            await source.stop()

    async def test_disconnect_reconnects_then_stop_cancels_active_socket(self):
        first, second = FakeSocket(block=False), FakeSocket()
        with patch("src.danmaku_source.connect", side_effect=[first, second]) as connection:
            source, _ = await self.source(enabled=True)
            await asyncio.wait_for(second.waiting.wait(), 2)
            self.assertEqual(connection.call_count, 2)
            self.assertEqual(source.status()["counts"]["disconnects"], 1)
            self.assertEqual(source.status()["counts"]["reconnects"], 1)
            await source.stop()
            self.assertGreaterEqual(second.close_calls, 1)

    async def test_connection_failure_backoff_can_be_cancelled_without_retry(self):
        with patch("src.danmaku_source.connect", side_effect=OSError("private-url")) as connection:
            source, _ = await self.source(enabled=True)
            # The first failure enters its one-second backoff without opening a socket.
            await asyncio.sleep(0)
            self.assertEqual(source.status()["state"], "reconnecting")
            self.assertEqual(source.status()["counts"]["connection_errors"], 1)
            self.assertNotIn("private-url", json.dumps(source.status()))
            task = source._task
            await source.stop()
            self.assertTrue(task.done())
            await asyncio.sleep(0)
            self.assertEqual(connection.call_count, 1)

    async def test_invalid_disabled_and_unsupported_input_do_not_dispatch(self):
        source, callback = await self.source()
        self.assertEqual(source.status()["state"], "disabled")
        for raw in ["not-json", "[]", event(data=None), event(event_id="empty", data={"content": " "}),
                    event("member", "member")]:
            await source._handle_message(raw)
        self.assertEqual(source.status()["counts"]["invalid"], 4)
        self.assertEqual(source.status()["counts"]["unsupported"], 1)
        callback.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
