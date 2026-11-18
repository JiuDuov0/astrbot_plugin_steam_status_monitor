"""AstrBot 插件兼容入口。

具体实现位于 ``src.plugin.steam_status_monitor``，根入口保留用于 AstrBot 插件发现。
"""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.star_handler import star_handlers_registry

from .src.plugin.steam_status_monitor import SteamStatusMonitorV3
from .src.application.services import llm_monitor, llm_queries
from .src.application.services.llm_permissions import authorize_command


class Main(SteamStatusMonitorV3):
    """AstrBot plugin entry point."""

    async def steam_owned_games(
        self,
        event: AstrMessageEvent,
        /,
        query: str = "",
        offset: int = 0,
        limit: int = 20,
        **_ignored,
    ) -> str:
        # LLM 工具注册桩必须留在入口模块：AstrBot 按 handler.__module__ 匹配
        # 插件模块路径并绑定插件实例；注册在 src/ 子模块里不会绑定，调用时
        # 会把 event 当成 self。官方机器人链路还可能额外传 event 关键字参数，
        # 这里用 **_ignored 吞掉，只保留位置参数里的真实事件对象。
        if not authorize_command(self, event, "steam_mygames"):
            return "权限不足或游戏库查询指令已禁用。"
        return await SteamStatusMonitorV3.steam_owned_games(
            self,
            event,
            query=query,
            offset=offset,
            limit=limit,
        )

    async def steam_monitor_query(
        self, event: AstrMessageEvent, /, action: str, argument: str = "", **_ignored,
    ) -> str:
        """查询 Steam 状态、游戏、价格、排行榜和插件配置，不执行管理操作。

        查询类型：list（本群状态）、who（绑定用户状态）、alllist（所有群状态）、
        rank（本群排行）、allrank（所有群排行）、rank_push_status（排行推送状态）、
        game（游戏详情）、price（价格）、openbox（Steam 玩家资料）、help（帮助）、
        achievement_blacklist（成就黑名单）、blacklist（游戏过滤名单）、
        fonts（字体状态）、qq_menu_status（QQ 面板状态）、config（安全配置）。
        查询本人游戏库请使用 steam_owned_games。所有群信息和管理员查询只允许管理员使用。

        Args:
            action(string): 工具说明中的查询类型
            argument(string): 游戏名、AppID、SteamID、绑定用户 ID、排行榜周期或页码；无参数时留空
        """
        return await llm_queries.query(self, event, action, argument)

    async def steam_monitor_add(
        self, event: AstrMessageEvent, /, steamid: str,
        user_id: str = "", nickname: str = "", **_ignored,
    ) -> str:
        """管理员在当前群添加 SteamID 到监控，可选择绑定用户。

        Args:
            steamid(string): SteamID、好友码或个人资料链接，多个用逗号分隔
            user_id(string): 可选的绑定用户 QQ 号或 OpenID
            nickname(string): 可选的绑定备注
        """
        return await llm_monitor.add_player(self, event, steamid, user_id, nickname)

    async def steam_monitor_remove(
        self, event: AstrMessageEvent, /, steamid: str, **_ignored,
    ) -> str:
        """管理员申请从当前群移除 SteamID；只发起申请，不直接删除。

        申请后必须将返回的确认指令原样展示给用户，由同一管理员在当前群发送。

        Args:
            steamid(string): 要移除的 SteamID、好友码或个人资料链接
        """
        return await llm_monitor.request_removal(self, event, steamid)

    async def terminate(self):
        # AstrBot 只调用入口类自身 __dict__ 里的 terminate，继承来的方法不会被调用。
        await super().terminate()


# AstrBot 通过模块路径把处理器绑定到插件实例。处理器定义在实现模块，
# 入口类定义在当前模块，因此需要将处理器归属调整到运行时入口模块。
_IMPLEMENTATION_MODULE = SteamStatusMonitorV3.__module__
for _handler in star_handlers_registry:
    if _handler.handler_module_path == _IMPLEMENTATION_MODULE:
        _handler.handler_module_path = __name__


# LLM 工具的 schema 来自函数 docstring；复用实现方法的 docstring，避免两处维护。
Main.steam_owned_games.__doc__ = SteamStatusMonitorV3.steam_owned_games.__doc__
Main.steam_owned_games = filter.llm_tool(name="steam_owned_games")(
    Main.steam_owned_games
)
Main.steam_monitor_query = filter.llm_tool(name="steam_monitor_query")(Main.steam_monitor_query)
Main.steam_monitor_add = filter.llm_tool(name="steam_monitor_add")(Main.steam_monitor_add)
Main.steam_monitor_remove = filter.llm_tool(name="steam_monitor_remove")(Main.steam_monitor_remove)


__all__ = ["Main", "SteamStatusMonitorV3"]
