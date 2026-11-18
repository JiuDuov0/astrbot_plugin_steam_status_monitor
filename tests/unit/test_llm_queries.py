"""LLM 查询的权限、只读参数及消息发送回归测试。"""
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.application.services import llm_queries


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, path):
        self.path = path


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


class Event:
    def __init__(self, admin=False):
        self.admin = admin
        self.message_str = "/steam BL del all"
        self.sent = []

    def is_admin(self):
        return self.admin

    def get_group_id(self):
        return "group-1"

    async def send(self, chain):
        self.sent.append(chain)

    def image_result(self, path):
        return MessageChain([Image(path)])


class Plugin:
    def __init__(self):
        self.calls = []
        self.config = {}

    def __getattr__(self, name):
        if not name.startswith("steam_"):
            raise AttributeError(name)

        async def handler(event, *args):
            self.calls.append((name, event.message_str, args))
            event.group_steam_ids = ["临时"]
            yield MessageChain([Plain("成功")])
        return handler


def fake_astrbot():
    modules = {}
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components"):
        modules[name] = types.ModuleType(name)
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].Image = Image
    return modules


class QueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.event = Event(admin=True)
        self.modules = patch.dict(sys.modules, fake_astrbot())
        self.modules.start()
        self.permission = patch.object(llm_queries, "authorize_command", return_value=True)
        self.auth = self.permission.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.permission.stop)

    async def test_all_commands_route_with_fixed_read_only_arguments(self):
        cases = {
            "alllist": ("", "steam_alllist", "", ("text",)),
            "openbox": ("12345678", "steam_openbox", "", ("12345678",)),
            "help": ("", "steam_help", "", ()),
            "rank_push_status": ("", "steam_rank_on", "", ("list",)),
            "achievement_blacklist": ("2", "steam_achievement_blacklist", "/steam achievement_blacklist list text 2", ("list", "text")),
            "blacklist": ("", "steam_blacklist", "/steam BL list", ("list", "")),
            "fonts": ("", "steam_fonts", "", ("status",)),
        }
        for action, (arg, name, message, args) in cases.items():
            with self.subTest(action=action):
                result = await llm_queries.query(self.plugin, self.event, action, arg)
                self.assertEqual("成功", result)
                self.assertEqual((name, message, args), self.plugin.calls[-1])
                self.assertEqual(name, self.auth.call_args.args[2])
        self.assertEqual("/steam BL del all", self.event.message_str)
        self.assertFalse(hasattr(self.event, "group_steam_ids"))

    async def test_admin_and_registry_denials_fail_closed(self):
        self.event.admin = False
        for action in ("alllist", "allrank", "rank_push_status", "achievement_blacklist", "blacklist", "fonts", "qq_menu_status"):
            self.assertIn("管理员", await llm_queries.query(self.plugin, self.event, action))
        self.assertFalse(self.plugin.calls)
        self.auth.return_value = False
        self.assertIn("无权", await llm_queries.query(self.plugin, self.event, "list"))
        self.assertFalse(self.plugin.calls)
        self.event.is_admin = None
        self.assertIn("管理员", await llm_queries.query(self.plugin, self.event, "alllist"))
        self.event.is_admin = lambda: (_ for _ in ()).throw(RuntimeError())
        self.assertIn("查询失败", await llm_queries.query(self.plugin, self.event, "alllist"))

    async def test_unsafe_subcommands_are_rejected(self):
        for action, arg in (("blacklist", "del all"), ("achievement_blacklist", "del all"),
                            ("achievement_blacklist", "names"), ("fonts", "download"),
                            ("alllist", "img"), ("rank", "test"), ("game", "730 del"),
                            ("rank_push_status", "test"), ("help", "other"), ("unknown", "")):
            with self.subTest(action=action, arg=arg):
                self.assertIn("查询失败", await llm_queries.query(self.plugin, self.event, action, arg))
        self.assertFalse(self.plugin.calls)

    async def test_image_is_sent_not_exposed_and_send_errors_are_explicit(self):
        async def handler(event, arg):
            yield MessageChain([Plain("详情"), Image("C:\\private\\secret.png")])
        self.plugin.steam_openbox = handler
        result = await llm_queries.query(self.plugin, self.event, "openbox", "12345678")
        self.assertEqual("详情\n已向当前会话发送 1 张图片。", result)
        self.assertEqual("C:\\private\\secret.png", self.event.sent[0].chain[0].path)
        async def fail(chain):
            raise RuntimeError("C:\\private\\secret.png")
        self.event.send = fail
        self.assertIn("查询失败", await llm_queries.query(self.plugin, self.event, "openbox", "12345678"))

    async def test_config_uses_explicit_safe_values_only(self):
        self.plugin.config = {
            "steam_api_key": "secret-A", "qq_official_secret": "secret-B",
            "proxy_url": "http://user:password@host", "nested": {"token": "secret-C"},
            "other": "secret-D", "price_currency": "secret-E", "enable_proxy": True,
            "retry_times": 4, "fixed_poll_interval": "secret-F",
        }
        result = await llm_queries.query(self.plugin, self.event, "config")
        for secret in ("secret-A", "secret-B", "password", "secret-C", "secret-D", "secret-E", "secret-F", "proxy_url"):
            self.assertNotIn(secret, result)
        self.assertIn("enable_proxy: True", result)
        self.assertIn("retry_times: 4", result)
        self.assertEqual("steam_config", self.auth.call_args.args[2])
        self.auth.return_value = False
        self.assertIn("无权", await llm_queries.query(self.plugin, self.event, "config"))

    async def test_qq_status_never_clears_missing_panel(self):
        self.plugin.config = {"qq_menu_panel_id": "saved"}
        class Client:
            async def get_panel(self, panel_id):
                raise RuntimeError("含密钥 secret")
        self.plugin._qq_menu_client = lambda event: (Client(), "qq_official")
        result = await llm_queries.query(self.plugin, self.event, "qq_menu_status")
        self.assertIn("失败", result)
        self.assertNotIn("secret", result)
        self.assertEqual("saved", self.plugin.config["qq_menu_panel_id"])
        self.assertEqual("steam_qq_menu_status", self.auth.call_args.args[2])

    async def test_unknown_result_is_failure_not_repr(self):
        async def handler(event, arg):
            yield "C:\\private\\secret.png"
        self.plugin.steam_openbox = handler
        result = await llm_queries.query(self.plugin, self.event, "openbox", "12345678")
        self.assertIn("查询失败", result)
        self.assertNotIn("secret", result)

    async def test_exception_text_and_local_paths_are_hidden(self):
        async def handler(event, arg):
            yield MessageChain([Plain("排行榜生成失败: 密钥 secret C:\\private\\file.png")])
            yield MessageChain([Plain("路径 C:\\private\\file.png 和 /home/user/image.png")])
        self.plugin.steam_openbox = handler
        result = await llm_queries.query(self.plugin, self.event, "openbox", "12345678")
        self.assertNotIn("secret", result)
        self.assertNotIn("private", result)
        self.assertNotIn("/home/user", result)

    async def test_player_data_uses_group_view_once_and_sends_same_rows(self):
        row = {"sid": "100", "name": "小明", "status": "playing", "game": "游戏", "gameid": "730", "play_str": "20分钟", "avatar_url": "C:\\private\\avatar.png"}
        view = types.SimpleNamespace(steam_ids_for_group=Mock(return_value=([], [], ["100"])),
                                     build_group_rows=AsyncMock(return_value=[row]))
        self.plugin.API_KEY = "secret-A"
        self.plugin.player_status_view = view
        image = AsyncMock(return_value=MessageChain([Image("C:\\private\\image.png")]))
        with patch.object(llm_queries, "_rows_image", image):
            result = await llm_queries.query(self.plugin, self.event, "list")
        data = json.loads(result.splitlines()[0])
        self.assertEqual("小明", data["玩家"][0]["name"])
        self.assertEqual("游戏", data["玩家"][0]["game"])
        self.assertNotIn("private", result)
        view.build_group_rows.assert_awaited_once_with("group-1")
        image.assert_awaited_once_with(self.plugin, self.event, [row])
        self.assertEqual(1, len(self.event.sent))
        self.assertEqual("steam_list", self.auth.call_args.args[2])

    async def test_who_fetches_binding_once_and_handles_missing(self):
        row = {"sid": "100", "name": "小明", "status": "online"}
        view = types.SimpleNamespace(primary_group_of=Mock(return_value="owner"), build_row=AsyncMock(return_value=row))
        self.plugin.player_status_view = view
        self.plugin._bind_data = {"123456": {"sid": "100"}}
        self.plugin.fetch_player_status = AsyncMock(return_value={"name": "小明"})
        with patch.object(llm_queries, "_rows_image", AsyncMock(return_value=None)):
            result = await llm_queries.query(self.plugin, self.event, "who", "123456")
            missing = await llm_queries.query(self.plugin, self.event, "who", "999999")
        self.assertEqual("小明", json.loads(result.splitlines()[0])["玩家"][0]["name"])
        self.assertIn("图片生成失败", result)
        self.assertIn("未绑定", missing)
        self.plugin.fetch_player_status.assert_awaited_once_with("100")
        view.build_row.assert_awaited_once_with("100", {"name": "小明"}, group_id="owner")

    async def test_rank_data_scope_and_single_aggregation(self):
        rows = [{"sid": "100", "name": "玩家", "total_minutes": 95,
                 "games": [{"gameid": "730", "name": "游戏", "minutes": 95}]}]
        ranking = types.SimpleNamespace(target_sids=Mock(return_value={"100"}), aggregate=Mock(return_value=rows))
        self.plugin.ranking_service = ranking
        self.plugin.rank_view = types.SimpleNamespace(enrich=AsyncMock(), render_file=AsyncMock(return_value=None))
        result = await llm_queries.query(self.plugin, self.event, "rank", "week")
        data = json.loads(result.splitlines()[0])
        self.assertEqual(95, data["排行榜"][0]["total_minutes"])
        self.assertEqual("游戏", data["排行榜"][0]["games"][0]["name"])
        ranking.target_sids.assert_called_once_with("group-1")
        ranking.aggregate.assert_called_once_with(days=7, sids={"100"}, base_day_offset=0)
        self.plugin.rank_view.enrich.assert_awaited_once()
        ranking.target_sids.reset_mock()
        await llm_queries.query(self.plugin, self.event, "allrank", "30")
        ranking.target_sids.assert_called_once_with(None)
        self.assertEqual("steam_allrank", self.auth.call_args.args[2])
        self.event.admin = False
        ranking.aggregate.reset_mock()
        self.assertIn("管理员", await llm_queries.query(self.plugin, self.event, "allrank"))
        ranking.aggregate.assert_not_called()

    async def test_game_and_price_return_card_data_without_refetching(self):
        card = types.SimpleNamespace(
            game=types.SimpleNamespace(appid="730", title="游戏"),
            detail={"name": "游戏", "release_date": {"date": "2020"}, "developers": ["开发者"],
                    "price_overview": {"currency": "CNY", "final_formatted": "¥50"},
                    "api_key": "secret-A", "header_image": "C:\\private\\image.png"},
            card_data={}, current_price={"value": 50, "currency": "CNY", "actual_region": "CN"},
            history_low={"value": 20, "currency": "CNY"}, region_prices={"US": {"current_price": 7, "currency": "USD"}},
            summary={}, locked=False,
        )
        service = types.SimpleNamespace(build_store_card=AsyncMock(return_value=card),
                                        resolve_games=AsyncMock(return_value={"games": [card.game]}),
                                        build_card=AsyncMock(return_value=card))
        self.plugin.price_query = service
        self.plugin.config = {"steam_api_key": "secret-A"}
        candidates = object()
        self.plugin.price_candidates = candidates
        with patch.object(llm_queries, "_card_image", AsyncMock(return_value=None)) as image:
            game = await llm_queries.query(self.plugin, self.event, "game", "730")
            price = await llm_queries.query(self.plugin, self.event, "price", "Game Name")
        self.assertEqual("游戏", json.loads(game.splitlines()[0])["name"])
        data = json.loads(price.splitlines()[0])
        self.assertEqual(50, data["current_price"]["value"])
        self.assertEqual(20, data["history_low"]["value"])
        self.assertEqual("USD", data["region_prices"][0]["currency"])
        self.assertNotIn("secret-A", game + price)
        self.assertNotIn("private", game + price)
        service.build_store_card.assert_awaited_once_with("730")
        service.resolve_games.assert_awaited_once_with("Game Name")
        service.build_card.assert_awaited_once_with(card.game)
        self.assertEqual(2, image.await_count)
        self.assertIs(self.plugin.price_candidates, candidates)
        self.assertEqual("/steam BL del all", self.event.message_str)
        self.assertEqual("steam_px", self.auth.call_args.args[2])

    async def test_query_errors_are_logged_without_exception_secrets(self):
        self.plugin.config = {"qq_menu_panel_id": "saved"}
        self.plugin._qq_menu_client = Mock(side_effect=RuntimeError("secret-A"))
        with patch.object(llm_queries.logger, "error") as log:
            qq = await llm_queries.query(self.plugin, self.event, "qq_menu_status")
            self.plugin.price_query = types.SimpleNamespace(build_store_card=AsyncMock(side_effect=RuntimeError("secret-B")))
            game = await llm_queries.query(self.plugin, self.event, "game", "730")
        self.assertIn("失败", qq)
        self.assertIn("失败", game)
        self.assertEqual(2, log.call_count)
        self.assertNotIn("secret", str(log.call_args_list))

    async def test_empty_results_and_rank_push_permissions(self):
        self.plugin.rank_view = types.SimpleNamespace(enrich=AsyncMock(), render_file=AsyncMock())
        self.plugin.ranking_service = types.SimpleNamespace(target_sids=Mock(return_value=set()), aggregate=Mock(return_value=[]))
        self.assertIn("暂无", await llm_queries.query(self.plugin, self.event, "rank"))
        self.plugin.rank_view.enrich.assert_not_awaited()
        self.plugin.price_query = types.SimpleNamespace(resolve_games=AsyncMock(return_value={"games": [], "status": "EMPTY"}),
                                                         _search_message=Mock(return_value="未找到匹配游戏。"), build_card=AsyncMock())
        self.assertIn("未找到", await llm_queries.query(self.plugin, self.event, "price", "missing"))
        self.plugin.price_query.build_card.assert_not_awaited()
        self.auth.return_value = False
        self.assertIn("无权", await llm_queries.query(self.plugin, self.event, "rank_push_status"))
        self.assertFalse(self.plugin.calls)

    async def test_rank_image_uses_same_rows_and_send_failure_preserves_data(self):
        rows = [{"sid": "1", "name": "玩家", "total_minutes": 60, "games": []}]
        self.plugin.ranking_service = types.SimpleNamespace(target_sids=Mock(return_value={"1"}), aggregate=Mock(return_value=rows))
        self.plugin.rank_view = types.SimpleNamespace(enrich=AsyncMock(), render_file=AsyncMock(return_value="C:\\private\\rank.png"))
        result = await llm_queries.query(self.plugin, self.event, "rank")
        self.assertEqual(60, json.loads(result.splitlines()[0])["排行榜"][0]["total_minutes"])
        self.assertIn("已向当前会话发送", result)
        self.assertNotIn("private", result)
        self.plugin.rank_view.render_file.assert_awaited_once_with(rows, "今日")
        async def fail(chain):
            raise RuntimeError("secret")
        self.event.send = fail
        with patch.object(llm_queries.logger, "error") as log:
            failed = await llm_queries.query(self.plugin, self.event, "rank")
        self.assertEqual(60, json.loads(failed.splitlines()[0])["排行榜"][0]["total_minutes"])
        self.assertIn("发送失败", failed)
        self.assertNotIn("secret", failed)
        log.assert_called_once()

    async def test_game_card_image_is_sent_as_base64(self):
        class Builder:
            def base64_image(self, value):
                return MessageChain([Image(value)])
        self.event.make_result = Builder
        self.plugin.proxy = None
        card = types.SimpleNamespace(detail={"name": "游戏"}, card_data={"name": "游戏"}, game=None)
        self.plugin.price_query = types.SimpleNamespace(build_store_card=AsyncMock(return_value=card))
        with patch("src.presentation.renderers.game_detail.render_game_detail_image", AsyncMock(return_value=b"png")) as render:
            result = await llm_queries.query(self.plugin, self.event, "game", "730")
        self.assertIn("游戏", result)
        self.assertIn("已向当前会话发送", result)
        self.assertEqual("cG5n", self.event.sent[0].chain[0].path)
        render.assert_awaited_once()
        self.plugin.price_query.build_store_card.assert_awaited_once_with("730")


if __name__ == "__main__":
    unittest.main()
