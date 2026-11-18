"""owned 通知接入：去重键、文本行、图片渲染分支。"""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.application.services.notification_tracking import NotificationTrackingMixin


class _FakeContext:
    def __init__(self):
        self.messages = []

    async def send_message(self, session, chain):
        self.messages.append((session, chain))


class _Stub(NotificationTrackingMixin):
    def __init__(self):
        self.config = {"notify_send_text": True, "notify_send_image": False}
        self.data_dir = "."
        self.API_KEY = "k"
        self.SGDB_API_KEY = ""
        self.SGDB_API_BASE = ""
        self.STEAM_STORE_BASE = ""
        self.STEAM_API_BASE = ""
        self.proxy = None
        self._plugin_version = "4.10.0"
        self.context = _FakeContext()

    def _get_notify_sessions(self, group_id, sid):
        return [f"session:{group_id}"]


def _chain_text(chain):
    parts = []
    for segment in getattr(chain, "chain", []):
        text = getattr(segment, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


class OwnedNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_event_key_uses_added_at(self):
        stub = _Stub()
        key = stub._notification_event_key(
            {"type": "owned", "sid": "s1", "gameid": "570", "added_at": 123}, "session:111"
        )
        self.assertEqual(key, ("session:111", "s1", "570", "owned", 123))

    def test_dedup_window(self):
        stub = _Stub()
        noti = {"type": "owned", "sid": "s1", "gameid": "570", "added_at": 123}
        self.assertTrue(stub._should_send_notification(noti, "session:111"))
        self.assertFalse(stub._should_send_notification(noti, "session:111"))

    async def test_text_line_for_owned(self):
        stub = _Stub()
        noti = {
            "type": "owned", "sid": "s1", "gameid": "570", "added_at": 1,
            "name": "阿猫", "game": "Dota 2", "owned_count": 268, "render": False,
        }

        failed = await stub._send_merged_notification("111", [noti])

        self.assertEqual(failed, [])
        self.assertEqual(len(stub.context.messages), 1)
        session, chain = stub.context.messages[0]
        self.assertEqual(session, "session:111")
        text = _chain_text(chain)
        self.assertIn("新入库", text)
        self.assertIn("Dota 2", text)
        self.assertIn("当前共 268 款", text)

    async def test_render_flag_false_skips_image(self):
        stub = _Stub()
        path = await stub._render_notification_image({"type": "owned", "render": False})
        self.assertIsNone(path)

    async def test_render_owned_calls_renderer(self):
        stub = _Stub()
        with patch(
            "src.application.services.notification_tracking.render_owned_game",
            new=AsyncMock(return_value=b"fake-png"),
        ) as mocked:
            path = await stub._render_notification_image({
                "type": "owned", "render": True, "sid": "s1", "name": "阿猫",
                "avatar_url": None, "gameid": "570", "game": "Dota 2",
                "owned_count": 3, "card_data": None, "added_at": 1,
            })

        self.assertEqual(mocked.await_count, 1)
        self.assertIsNotNone(path)
        try:
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), b"fake-png")
        finally:
            os.unlink(path)

    def test_removed_event_keys_and_dedup(self):
        stub = _Stub()
        single = {"type": "removed", "sid": "s1", "gameid": "570", "added_at": 123}
        batch = {"type": "removed_batch", "sid": "s1", "added_at": 123, "page": 1, "pages": 2}
        self.assertEqual(stub._notification_event_key(single, "session"),
                         ("session", "s1", "570", "removed", 123))
        self.assertEqual(stub._notification_event_key(batch, "session"),
                         ("session", "s1", "p1/2", "removed_batch", 123))
        for item in (single, batch):
            self.assertTrue(stub._should_send_notification(item, "session"))
            self.assertFalse(stub._should_send_notification(item, "session"))
        self.assertTrue(stub._should_send_notification(dict(single, added_at=124), "session"))
        self.assertTrue(stub._should_send_notification(dict(single, type="owned"), "session"))
        self.assertTrue(stub._should_send_notification(dict(batch, page=2), "session"))
        self.assertTrue(stub._should_send_notification(batch, "another-session"))

    async def test_removed_text_and_batch_pagination(self):
        stub = _Stub()
        single = dict(type="removed", sid="s1", gameid="570", added_at=1,
                      name="阿猫", game="Dota 2", owned_count=2)
        batch = dict(type="removed_batch", sid="s1", added_at=1, name="阿猫",
                     owned_count=2, added_count=3, items=[{"appid": "1"}], page=1, pages=2)
        self.assertEqual([], await stub._send_merged_notification("111", [single, batch]))
        text = _chain_text(stub.context.messages[0][1])
        self.assertIn("移出游戏库 Dota 2（当前共 2 款）", text)
        self.assertIn("移出游戏库 3 款（当前共 2 款）", text)
        self.assertIn("第 1/2 页", text)
        self.assertNotIn("退款", text)
        self.assertEqual([], await stub._send_merged_notification("111", [single, batch]))
        self.assertEqual(1, len(stub.context.messages))

    async def test_removed_render_routes_and_flags(self):
        stub = _Stub()
        single = dict(type="removed", sid="s1", name="p", gameid="570", game="g", added_at=123)
        batch = dict(type="removed_batch", sid="s1", name="p", items=[{"appid": "570"}],
                     added_count=1, added_at=123, page=2, pages=3)
        temporary = MagicMock()
        temporary.__enter__.return_value.name = "image.png"
        with patch("src.application.services.notification_tracking.tempfile.NamedTemporaryFile",
                   return_value=temporary), \
             patch("src.application.services.notification_tracking.render_owned_game",
                   new=AsyncMock(return_value=b"png")) as single_renderer, \
             patch("src.application.services.notification_tracking.render_owned_games_batch",
                   new=AsyncMock(return_value=b"png")) as batch_renderer:
            self.assertEqual("image.png", await stub._render_notification_image(single))
            self.assertEqual("image.png", await stub._render_notification_image(batch))
            self.assertIsNone(await stub._render_notification_image(dict(single, render=False)))
        self.assertTrue(single_renderer.await_args.kwargs["removed"])
        self.assertEqual(123, single_renderer.await_args.kwargs["added_at"])
        self.assertTrue(batch_renderer.await_args.kwargs["removed"])
        self.assertEqual(batch["items"], batch_renderer.await_args.kwargs["items"])
        self.assertEqual(2, batch_renderer.await_args.kwargs["page"])

    async def test_removed_no_image_and_retry_behavior(self):
        for kind in ("removed", "removed_batch"):
            with self.subTest(kind=kind):
                stub = _Stub()
                stub.config = {"notify_send_text": False, "notify_send_image": True}
                item = dict(type=kind, sid="s1", gameid="570", added_at=1,
                            name="p", game="g", items=[], render=False)
                stub._render_notification_image = AsyncMock(return_value=None)
                self.assertEqual([], await stub._send_merged_notification("111", [item]))
                self.assertEqual([], stub.context.messages)
                self.assertFalse(stub._inflight_notification_events)
                failing = dict(item, added_at=2, render=True)
                failed = await stub._send_merged_notification("111", [failing])
                self.assertEqual([dict(failing, _target_session="session:111")], failed)
                self.assertFalse(stub._inflight_notification_events)
                self.assertNotIn("session:111", stub._notification_cooldowns)
                stub.config["notify_send_text"] = True
                self.assertEqual([], await stub._send_merged_notification("111", failed))
                self.assertEqual(1, len(stub.context.messages))

    async def test_removed_image_only_is_sent_and_deduplicated(self):
        for kind in ("removed", "removed_batch"):
            with self.subTest(kind=kind):
                stub = _Stub()
                stub.config = {"notify_send_text": False, "notify_send_image": True}
                stub._render_notification_image = AsyncMock(return_value="image.png")
                item = dict(type=kind, sid="s1", gameid="570", added_at=1,
                            name="p", game="g", items=[])
                with patch("src.application.services.notification_tracking.Image.fromFileSystem",
                           return_value=MagicMock()) as image:
                    self.assertEqual([], await stub._send_merged_notification("111", [item]))
                    self.assertEqual([], await stub._send_merged_notification("111", [item]))
                image.assert_called_once_with("image.png")
                stub._render_notification_image.assert_awaited_once()
                self.assertEqual(1, len(stub.context.messages))

    async def test_removed_bypasses_owned_cooldown_and_recovers_send_failure(self):
        for kind in ("removed", "removed_batch"):
            with self.subTest(kind=kind):
                stub = _Stub()
                stub._notification_cooldowns = {"session:111": (1, float("inf"))}
                item = dict(type=kind, sid="s1", gameid="570", added_at=1,
                            name="p", game="g", items=[])
                stub.context.send_message = AsyncMock(side_effect=RuntimeError("offline"))
                failed = await stub._send_merged_notification("111", [item])
                self.assertEqual([dict(item, _target_session="session:111")], failed)
                self.assertEqual((1, float("inf")), stub._notification_cooldowns["session:111"])
                self.assertFalse(stub._inflight_notification_events)
                stub.context.send_message = AsyncMock()
                self.assertEqual([], await stub._send_merged_notification("111", failed))
                stub.context.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
