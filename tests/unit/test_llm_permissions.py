import sys
import types
import unittest
from unittest.mock import patch

from src.application.services.llm_permissions import authorize_command


class PermissionFilter:
    def __init__(self, allowed):
        self.allowed = allowed

    def filter(self, event, config):
        return self.allowed


class Plugin:
    pass


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.handler = types.SimpleNamespace(
            handler_module_path=Plugin.__module__, handler_name="steam_addid",
            enabled=True, event_filters=[PermissionFilter(True)],
        )
        permission = types.ModuleType("astrbot.core.star.filter.permission")
        permission.PermissionTypeFilter = PermissionFilter
        registry = types.ModuleType("astrbot.core.star.star_handler")
        registry.star_handlers_registry = [self.handler]
        self.modules = patch.dict(sys.modules, {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.core": types.ModuleType("astrbot.core"),
            "astrbot.core.star": types.ModuleType("astrbot.core.star"),
            "astrbot.core.star.filter": types.ModuleType("astrbot.core.star.filter"),
            "astrbot.core.star.filter.permission": permission,
            "astrbot.core.star.star_handler": registry,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_registered_enabled_command_uses_live_permission(self):
        self.assertTrue(authorize_command(Plugin(), object(), "steam_addid"))
        self.handler.event_filters[0].allowed = False
        self.assertFalse(authorize_command(Plugin(), object(), "steam_addid"))
        self.handler.event_filters[0].allowed = True
        self.handler.enabled = False
        self.assertFalse(authorize_command(Plugin(), object(), "steam_addid"))

    def test_missing_handler_or_permission_fails_closed(self):
        self.assertFalse(authorize_command(Plugin(), object(), "steam_delid"))
        self.handler.event_filters = []
        self.assertFalse(authorize_command(Plugin(), object(), "steam_addid"))


if __name__ == "__main__":
    unittest.main()