"""面向 LLM 的只读查询适配，权限始终以原始事件和命令注册信息为准。"""

import base64
import inspect
import json
import re

from .llm_permissions import authorize_command
from ...shared.logging import logger, redact_sensitive


# 动作到原命令的映射；不得从输入动态解析处理器名或子命令。
_ACTIONS = {
    "list": ("steam_list", False),
    "who": ("steam_who", False),
    "alllist": ("steam_alllist", True),
    "rank": ("steam_rank", False),
    "allrank": ("steam_allrank", True),
    "rank_push_status": ("steam_rank_on", True),
    "game": ("steam_game", False),
    "price": ("steam_px", False),
    "openbox": ("steam_openbox", False),
    "help": ("steam_help", False),
    "achievement_blacklist": ("steam_achievement_blacklist", True),
    "blacklist": ("steam_blacklist", True),
    "fonts": ("steam_fonts", True),
    "qq_menu_status": ("steam_qq_menu_status", True),
    "config": ("steam_config", False),
}

# 配置值不能仅按名称脱敏：未知配置、嵌套对象和 URL 均可能包含密钥。
_CONFIG_NUMBERS = {
    "fixed_poll_interval", "retry_times", "max_group_size",
    "font_download_timeout_sec", "cache_avatar_hours", "cache_avatar_frame_hours",
    "cache_cover_vertical_hours", "cache_cover_horizontal_hours", "rank_push_hour",
    "rank_push_minute", "owned_games_removed_confirmations", "owned_games_poll_interval",
    "owned_games_max_per_notify", "owned_games_daily_budget", "owned_games_grace_days",
    "owned_games_auto_compact_threshold", "owned_games_compact_page_size",
    "owned_games_compact_max_pages", "owned_games_compact_cover_concurrency",
}
_CONFIG_BOOLEANS = {
    "detailed_poll_log", "enable_achievement_poll", "enable_steam_style",
    "enable_game_start_notify", "enable_game_end_notify",
    "enable_network_fluctuation_notify", "enable_user_online_notify",
    "enable_user_offline_notify", "notify_send_image", "notify_send_text",
    "enable_proxy", "font_download_enabled", "qq_official_enabled", "qq_menu_enabled",
    "enable_owned_games_notify", "enable_owned_games_removed_notify", "owned_games_include_free",
}
_CONFIG_ENUMS = {
    "price_currency": {"CNY", "JPY", "USD", "EUR", "GBP", "KRW", "RUB", "UAH", "TRY", "PLN", "BRL", "INR", "HKD", "TWD"},
    "price_region": {"CN", "US", "JP", "KR", "RU", "UA", "TR", "HK", "TW"},
    "price_compare_regions": {"NONE", "UA", "US", "JP", "KR", "RU", "CN", "TR", "HK", "TW"},
    "game_filter_mode": {"全部游戏", "黑名单", "白名单"},
    "qq_menu_scope": {"group", "c2c"},
}


class _EventView:
    """独立消息视图，命令写入的临时属性不会污染原事件。"""

    def __init__(self, event, message):
        self._event = event
        self.message_str = message

    def get_message_str(self):
        return self.message_str

    def __getattr__(self, name):
        return getattr(self._event, name)


def _config_text(plugin):
    config = plugin.config
    lines = []
    for key in sorted(_CONFIG_NUMBERS | _CONFIG_BOOLEANS | set(_CONFIG_ENUMS)):
        if key not in config:
            continue
        value = config[key]
        if key in _CONFIG_BOOLEANS and type(value) is bool:
            lines.append(f"{key}: {value}")
        elif key in _CONFIG_NUMBERS and type(value) in (int, float):
            lines.append(f"{key}: {value}")
        elif key in _CONFIG_ENUMS and isinstance(value, str) and value in _CONFIG_ENUMS[key]:
            lines.append(f"{key}: {value}")
    return "当前配置（仅显示安全项目，其余已隐藏）：\n" + ("\n".join(lines) or "无可显示项目。")


async def _qq_status(plugin, event):
    """只读取远端状态；原命令在面板不存在时会清除本地配置。"""
    panel_id = str(plugin.config.get("qq_menu_panel_id", "")).strip()
    if not panel_id:
        return "尚未记录 QQ 指令面板。"
    try:
        client, platform_type = plugin._qq_menu_client(event)
        data = await client.get_panel(panel_id)
        panel = data.get("panel") if isinstance(data.get("panel"), dict) else {}
        items = panel.get("items") if isinstance(panel.get("items"), list) else []
        return f"QQ 指令面板状态：\n平台：{platform_type}\n指令数：{len(items)}"
    except Exception as exc:
        logger.error("[LLM 查询] QQ 菜单状态异常：%s", type(exc).__name__)
        return "查询 QQ 指令面板失败；本地配置未更改。"


def _safe_text(text):
    """不将原命令的异常详情或本地图片路径反馈给模型。"""
    text = str(text)
    for prefix in ("排行榜生成失败", "游戏详情获取成功，但卡片生成失败", "Steam API 请求失败"):
        if text.startswith(prefix):
            return prefix + "，请稍后重试。"
    text = re.sub(r"(?i)(?:file://\S+|\b[a-z]:[\\/][^\s，。；]+|\\\\[^\s，。；]+)", "[本地路径已隐藏]", text)
    return re.sub(r"(?<![\w:/])/(?:[\w.-]+/)+[\w.\-/]+", "[本地路径已隐藏]", text)


def _data_text(plugin, value):
    """对明确选择的查询字段输出 JSON，移除地址和已配置的敏感值。"""
    def clean(item):
        if isinstance(item, dict):
            return {key: clean(val) for key, val in item.items()}
        if isinstance(item, list):
            return [clean(val) for val in item]
        if not isinstance(item, str):
            return item if item is None or type(item) in (bool, int, float) else None
        text = _safe_text(redact_sensitive(item))
        text = re.sub(r"(?i)\b(?:https?|socks5?)://\S+", "[地址已隐藏]", text)
        text = re.sub(r"(?<![\w:/])/[\w.-]+(?![\w./-])", "[本地路径已隐藏]", text)
        config = getattr(plugin, "config", {}) or {}
        for key, secret in config.items():
            if re.search(r"(?i)(api.?key|secret|token|password|proxy.?url)", str(key)) and isinstance(secret, str) and secret:
                text = text.replace(secret, "******")
        return text

    return json.dumps(clean(value), ensure_ascii=False)


def _player_data(row):
    return {key: row.get(key) for key in ("sid", "name", "status", "game", "gameid", "play_str", "lastlogoff")}


async def _image_note(event, make_result):
    """图片只是附加展示；失败不覆盖已经获取的查询数据。"""
    try:
        result = await make_result()
        if result is None:
            return "图片生成失败，已返回文本数据。"
        async def results():
            yield result
        return await _collect(event, results())
    except Exception as exc:
        logger.error("[LLM 查询] 图片生成或发送异常：%s", type(exc).__name__)
        return "图片生成或发送失败，已返回文本数据。"


async def _rows_image(plugin, event, rows):
    from .steam_list import render_user_list_image
    from ...shared.fonts import resolve_font_path

    path = await render_user_list_image(plugin, event, rows, font_path=resolve_font_path("NotoSansHans-Regular.otf"), proxy=plugin.proxy)
    return event.image_result(path) if path else None


async def _card_image(plugin, event, card, with_prices):
    from ...presentation.renderers.game_detail import render_game_detail_image
    from ...shared.fonts import resolve_font_path

    kwargs = {}
    if with_prices:
        kwargs = {"itad_summary": card.summary, "region_prices": card.region_prices,
                  "current_price": card.current_price, "history_low": card.history_low}
    image = await render_game_detail_image(card.card_data, font_path=resolve_font_path("NotoSansHans-Regular.otf"), proxy=plugin.proxy, **kwargs)
    if not image:
        return None
    return event.make_result().base64_image(base64.b64encode(image).decode("ascii"))


def _card_data(card, appid=None):
    detail = card.detail or card.card_data or {}
    game = getattr(card, "game", None)
    release = detail.get("release_date") or {}
    price = detail.get("price_overview") or {}
    description = re.sub(r"<[^>]*>", "", str(detail.get("short_description") or ""))[:500]
    data = {
        "appid": appid or getattr(game, "appid", None) or detail.get("steam_appid"),
        "name": detail.get("name") or getattr(game, "title", ""),
        "release_date": release.get("date") if isinstance(release, dict) else None,
        "developers": detail.get("developers") if isinstance(detail.get("developers"), list) else [],
        "description": description,
        "genres": [item.get("description") for item in (detail.get("genres") or []) if isinstance(item, dict)],
        "is_free": detail.get("is_free"),
        "steam_price": {key: price.get(key) for key in ("currency", "final_formatted", "initial_formatted", "discount_percent")},
    }
    return data


async def _data_query(plugin, event, action, arg):
    """共享同一份读模型生成文本和图片，不再次查询状态、排行或价格。"""
    if action in {"list", "who"}:
        getter = getattr(event, "get_group_id", None)
        group_id = str((getter() if callable(getter) else None) or "default")
        view = plugin.player_status_view
        if action == "list":
            if not plugin.API_KEY:
                return "未配置 Steam API Key，请先在插件配置中填写 steam_api_key。"
            if not view.steam_ids_for_group(group_id)[2]:
                return "本群未设置监控的 SteamID 列表，请先添加。"
            rows = await view.build_group_rows(group_id)
        else:
            from ...shared.utils.mentions import parse_mentioned_user_id
            user = parse_mentioned_user_id(arg, event=_EventView(event, ""))
            if not user:
                return "请提供有效的 QQ 号或官方机器人用户 OpenID。"
            binding = getattr(plugin, "_bind_data", {}).get(user)
            if not binding:
                return f"用户 {user} 未绑定 SteamID。"
            sid = binding.get("sid", "")
            if not sid:
                return "绑定数据异常。"
            status = await plugin.fetch_player_status(sid)
            if not status:
                return "无法获取绑定玩家的 Steam 状态。"
            primary = view.primary_group_of(sid, group_id)
            rows = [await view.build_row(sid, status, group_id=primary)]
        data = _data_text(plugin, {"范围": "本群" if action == "list" else "绑定成员", "玩家": [_player_data(row) for row in rows]})
        note = await _image_note(event, lambda: _rows_image(plugin, event, rows))
        return data + "\n" + note

    if action in {"rank", "allrank"}:
        from .rank_view import parse_rank_period
        days, label = parse_rank_period(arg)
        group_id = None if action == "allrank" else (event.get_group_id() or "default")
        ranking = plugin.ranking_service
        rows = ranking.aggregate(days=days, sids=ranking.target_sids(group_id), base_day_offset=0)
        if not rows:
            return f"暂无{label}游玩记录，玩家游戏结束后才会有数据。"
        await plugin.rank_view.enrich(rows, days=days, base_day_offset=0)
        result = [{"sid": row.get("sid"), "name": row.get("name"), "total_minutes": row.get("total_minutes"),
                   "games": [{key: game.get(key) for key in ("gameid", "name", "minutes")} for game in row.get("games", [])]} for row in rows]
        data = _data_text(plugin, {"范围": "全群" if group_id is None else "本群", "周期": label, "排行榜": result})
        async def image():
            path = await plugin.rank_view.render_file(rows, label)
            return event.image_result(path) if path else None
        return data + "\n" + await _image_note(event, image)

    service = plugin.price_query
    if action == "game":
        card = await service.build_store_card(arg)
        if not card or not card.detail:
            return f"未找到 Steam 游戏 AppID：{arg}，或 Steam 商店暂时无法访问。"
    else:
        resolved = await service.resolve_games(arg)
        games = list(resolved.get("games") or [])
        if not games:
            return _safe_text(service._search_message(resolved.get("status")))
        card = await service.build_card(games[0])
    data = _card_data(card, arg if action == "game" else None)
    if action == "price":
        data["current_price"] = {key: (card.current_price or {}).get(key) for key in ("value", "regular", "currency", "cut", "source", "requested_region", "actual_region", "is_fallback")}
        data["history_low"] = {key: (card.history_low or {}).get(key) for key in ("value", "currency", "cut", "source")}
        data["region_prices"] = [{"region": region, **{key: info.get(key) for key in ("current_price", "current_regular", "currency", "cut", "actual_region", "is_fallback")}} for region, info in (card.region_prices or {}).items() if isinstance(info, dict)]
        data["locked"] = card.locked
    text = _data_text(plugin, data)
    return text + "\n" + await _image_note(event, lambda: _card_image(plugin, event, card, action == "price"))


async def _collect(event, results):
    """返回文本并向当前会话发送图片，不把图片地址交给 LLM。"""
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image, Plain

    texts = []
    sent = 0
    async for result in results:
        chain = getattr(result, "chain", None)
        if not isinstance(chain, (list, tuple)):
            raise ValueError("不支持的命令结果")
        images = []
        for part in chain:
            if isinstance(part, Plain):
                texts.append(_safe_text(part.text))
            elif isinstance(part, Image):
                images.append(part)
            else:
                raise ValueError("不支持的消息类型")
        if images:
            await event.send(MessageChain(images))
            sent += len(images)
    if sent:
        texts.append(f"已向当前会话发送 {sent} 张图片。")
    return "\n".join(texts) if texts else "查询失败：命令未返回结果。"


async def query(plugin, event, action: str, argument: str = "") -> str:
    """执行白名单查询；参数仅表示目标、游戏、排行榜天数或成就名单页码。"""
    if not isinstance(action, str) or not isinstance(argument, str):
        return "查询失败：动作和参数必须是字符串。"
    action = action.strip().lower()
    if action.startswith("steam_"):
        action = action[6:]
    if action == "px":
        action = "price"
    spec = _ACTIONS.get(action)
    if not spec:
        return "查询失败：不支持该查询动作。"
    handler_name, admin_required = spec
    try:
        if admin_required:
            is_admin = getattr(event, "is_admin", None)
            if not callable(is_admin) or is_admin() is not True:
                return "查询失败：该操作仅限管理员。"
        allowed = authorize_command(plugin, event, handler_name)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True:
            return "查询失败：命令未启用或无权使用。"

        arg = argument.strip()
        if len(arg) > 500 or any(ord(c) < 32 for c in arg):
            return "查询失败：参数格式无效。"
        no_arg = {"list", "alllist", "rank_push_status", "help", "blacklist", "fonts", "qq_menu_status", "config"}
        if action in no_arg and arg:
            return "查询失败：该操作不接受参数。"
        if action in {"who", "game", "price", "openbox"} and not arg:
            return "查询失败：请提供查询目标。"
        if action == "game" and not re.fullmatch(r"[0-9]{1,10}", arg):
            return "查询失败：游戏详情需要数字 AppID。"
        if action in {"rank", "allrank"} and arg and not (arg in {"week", "month"} or (re.fullmatch(r"[0-9]{1,4}", arg) and 1 <= int(arg) <= 3650)):
            return "查询失败：排行榜参数应为 week、month 或 1 至 3650 的天数。"
        if action == "achievement_blacklist" and arg and not (re.fullmatch(r"[0-9]{1,6}", arg) and int(arg) > 0):
            return "查询失败：成就黑名单参数应为正整数页码。"

        if action == "config":
            return _config_text(plugin)
        if action == "qq_menu_status":
            return await _qq_status(plugin, event)
        if action in {"list", "who", "rank", "allrank", "game", "price"}:
            return await _data_query(plugin, event, action, arg)

        message = ""
        args = ()
        if action == "alllist":
            args = ("text",)
        elif action == "openbox":
            args = (arg,)
        elif action == "rank_push_status":
            args = ("list",)
        elif action == "blacklist":
            message = "/steam BL list"
            args = ("list", "")
        elif action == "achievement_blacklist":
            message = "/steam achievement_blacklist list text" + (f" {arg}" if arg else "")
            args = ("list", "text")
        elif action == "fonts":
            args = ("status",)
        view = _EventView(event, message)
        results = getattr(plugin, handler_name)(view, *args)
        return await _collect(event, results)
    except Exception as exc:
        logger.error("[LLM 查询] %s 异常：%s", action, type(exc).__name__)
        return "查询失败：服务或消息发送异常，请稍后重试。"
