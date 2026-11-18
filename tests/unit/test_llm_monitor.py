import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.application.services import llm_monitor

SID = "76561198000000001"


class Event:
    def __init__(self, *, admin=True, group="12345", sender="54321", origin=None, message=""):
        self.admin = admin
        self.group = group
        self.sender = sender
        self.unified_msg_origin = origin or f"platform:GroupMessage:0_{group}"
        self.message = message

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_message_str(self):
        return self.message


class Admin:
    def __init__(self):
        self.groups = {"12345": [SID]}
        self.removed = []
        self.added = []

    def remove_player(self, group, sid):
        self.removed.append((group, sid))
        return SimpleNamespace(changed=True, message="removed")

    def add_players(self, group, sids, **kwargs):
        self.added.append((group, sids, kwargs))
        return SimpleNamespace(started=False, message="added")


class Plugin:
    def __init__(self):
        self.monitor_admin = Admin()
        self.push_groups = {}

    async def resolve_steam_input(self, value):
        return value if value == SID else None


class MonitorToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.authorize = patch.object(llm_monitor, "authorize_command", return_value=True)
        self.authorize.start()
        self.addCleanup(self.authorize.stop)
        self.event = Event()

    async def _request(self):
        reply = await llm_monitor.request_removal(self.plugin, self.event, SID)
        token = self.plugin._llm_pending_removals[(self.event.unified_msg_origin, "54321", "12345")]["token"]
        self.assertIn("尚未移除", reply)
        self.assertIn(token, reply)
        return token

    async def test_admin_add_and_permission_denial(self):
        reply = await llm_monitor.add_player(self.plugin, self.event, SID)
        self.assertEqual(reply, "added")
        self.assertEqual(self.plugin.monitor_admin.added[0][1], [SID])
        denied = await llm_monitor.add_player(self.plugin, Event(admin=False), SID)
        self.assertIn("权限不足", denied)
        self.assertEqual(len(self.plugin.monitor_admin.added), 1)

    async def test_request_does_not_remove_and_confirm_is_single_use(self):
        token = await self._request()
        self.assertEqual(self.plugin.monitor_admin.removed, [])
        event = Event(message=f"/steam confirm_remove {token}")
        self.assertIn("已从群", llm_monitor.confirm_removal(self.plugin, event, token))
        self.assertEqual(self.plugin.monitor_admin.removed, [("12345", SID)])
        self.assertIn("没有匹配", llm_monitor.confirm_removal(self.plugin, event, token))

    async def test_confirmation_requires_admin_sender_session_and_explicit_command(self):
        token = await self._request()
        text = f"/steam confirm_remove {token}"
        for event in (Event(admin=False, message=text), Event(sender="99999", message=text),
                      Event(group="99999", message=text), Event(message="请确认删除")):
            llm_monitor.confirm_removal(self.plugin, event, token)
        self.assertEqual(self.plugin.monitor_admin.removed, [])
        self.assertIn("已从群", llm_monitor.confirm_removal(self.plugin, Event(message=text), token))

    async def test_expired_request_and_changed_routes_rejected(self):
        token = await self._request()
        identity = (self.event.unified_msg_origin, "54321", "12345")
        self.plugin._llm_pending_removals[identity]["expires"] = 0
        self.assertIn("过期", llm_monitor.confirm_removal(self.plugin, Event(message=f"/steam confirm_remove {token}"), token))
        token = await self._request()
        self.plugin.push_groups[SID] = ["22222"]
        self.assertIn("发生变化", llm_monitor.confirm_removal(self.plugin, Event(message=f"/steam confirm_remove {token}"), token))
        self.assertEqual(self.plugin.monitor_admin.removed, [])

    async def test_disabled_command_and_other_group_are_rejected(self):
        with patch.object(llm_monitor, "authorize_command", return_value=False):
            self.assertIn("权限不足", await llm_monitor.request_removal(self.plugin, self.event, SID))
        self.assertIn("不在本群", await llm_monitor.request_removal(self.plugin, Event(group="99999"), SID))
        self.assertEqual(self.plugin.monitor_admin.removed, [])

    async def test_permission_rechecked_when_confirming(self):
        token = await self._request()
        event = Event(message=f"/steam confirm_remove {token}")
        with patch.object(llm_monitor, "authorize_command", return_value=False):
            self.assertIn("权限不足", llm_monitor.confirm_removal(self.plugin, event, token))
        self.assertEqual(self.plugin.monitor_admin.removed, [])

    async def test_token_parameter_must_match_real_message(self):
        token = await self._request()
        event = Event(message="/steam confirm_remove 0000000000000000")
        self.assertIn("完整的确认指令", llm_monitor.confirm_removal(self.plugin, event, token))
        self.assertEqual(self.plugin.monitor_admin.removed, [])


if __name__ == "__main__":
    unittest.main()