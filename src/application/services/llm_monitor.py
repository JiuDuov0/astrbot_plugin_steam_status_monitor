"""LLM 监控名单管理：管理员添加，以及同会话二次确认移除。"""
import re
import secrets
import time

from .llm_permissions import authorize_command
from ...shared.utils.mentions import is_valid_user_id
from ...shared.utils.notify_session import is_valid_group_id

CONFIRM_TTL_SECONDS = 300


def _identity(event):
    sender = str(event.get_sender_id() or "").strip()
    group = str(event.get_group_id() or "").strip()
    origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
    if not sender or not origin or not is_valid_group_id(group):
        return None
    return origin, sender, group


def _authorized(plugin, event, handler_name):
    return bool(event.is_admin()) and authorize_command(plugin, event, handler_name)


async def add_player(plugin, event, steamid, user_id="", nickname=""):
    if not _authorized(plugin, event, "steam_addid"):
        return "权限不足或添加指令已禁用。"
    identity = _identity(event)
    if identity is None:
        return "请在有效的群聊会话中添加 SteamID。"
    user_id = str(user_id or "").strip().lstrip("@")
    if user_id and not is_valid_user_id(user_id):
        return "绑定用户 ID 格式无效。"
    inputs = [part.strip() for part in re.split(r"[,，]+", str(steamid or "")) if part.strip()]
    if not inputs:
        return "请提供 SteamID、好友码或个人资料链接。"
    resolved = []
    for raw in inputs:
        sid = await plugin.resolve_steam_input(raw)
        if not sid or not sid.isdigit() or len(sid) != 17:
            return f"无法解析为有效 SteamID：{raw}"
        if sid not in resolved:
            resolved.append(sid)
    result = plugin.monitor_admin.add_players(
        identity[2], resolved, bind_qq=user_id or None,
        bind_nickname=str(nickname or "").strip() or None,
        notify_session=identity[0],
    )
    if result.started:
        plugin._record_platform_id(event)
    return result.message


def _relationship(plugin, group, sid):
    groups = plugin.monitor_admin.groups
    owner = next((str(gid) for gid, ids in groups.items() if sid in ids), None)
    targets = tuple(str(gid) for gid in (getattr(plugin, "push_groups", {}) or {}).get(sid, []))
    if group != owner and group not in targets:
        return None
    return owner, targets


async def request_removal(plugin, event, steamid):
    if not _authorized(plugin, event, "steam_delid"):
        return "权限不足或移除指令已禁用。"
    identity = _identity(event)
    if identity is None:
        return "请在有效的群聊会话中申请移除 SteamID。"
    sid = await plugin.resolve_steam_input(str(steamid or "").strip())
    if not sid or not sid.isdigit() or len(sid) != 17:
        return "无法解析为有效 SteamID，请提供 SteamID、好友码或个人资料链接。"
    relationship = _relationship(plugin, identity[2], sid)
    if relationship is None:
        return "该 SteamID 不在本群的监控或分发路由中。"
    pending = getattr(plugin, "_llm_pending_removals", None)
    if pending is None:
        pending = plugin._llm_pending_removals = {}
    now = time.monotonic()
    for key, request in list(pending.items()):
        if request["expires"] <= now:
            del pending[key]
    token = secrets.token_hex(8)
    pending[identity] = {
        "sid": sid, "relationship": relationship,
        "token": token, "expires": now + CONFIRM_TTL_SECONDS,
    }
    return (
        f"尚未移除。请确认从群 {identity[2]} 移除 SteamID {sid}。"
        "若该群为主监控群，可能晋升其他分发群；若没有其他路由，将清理该玩家的绑定、状态和游戏库快照。"
        f"请由本次申请的管理员在本群 5 分钟内发送：/steam confirm_remove {token}"
    )


def confirm_removal(plugin, event, token):
    if not _authorized(plugin, event, "steam_delid"):
        return "权限不足或移除指令已禁用。"
    identity = _identity(event)
    if identity is None:
        return "请在申请移除的群聊会话中确认。"
    text = str(event.get_message_str() or "").strip()
    match = re.fullmatch(r"/steam\s+confirm_remove\s+([0-9a-f]{16})", text)
    if not match or not secrets.compare_digest(match.group(1), str(token or "")):
        return "请直接发送完整的确认指令。"
    pending = getattr(plugin, "_llm_pending_removals", {})
    request = pending.get(identity)
    if request is None or not secrets.compare_digest(str(token or ""), request["token"]):
        return "没有匹配的待确认移除请求。"
    del pending[identity]
    if request["expires"] <= time.monotonic():
        return "移除确认已过期，请重新申请。"
    sid = request["sid"]
    if _relationship(plugin, identity[2], sid) != request["relationship"]:
        return "监控关系已发生变化，请重新申请移除。"
    result = plugin.monitor_admin.remove_player(identity[2], sid)
    if not result.changed:
        return f"移除未完成：{result.message}"
    return f"已从群 {identity[2]} 移除 SteamID {sid} 的监控关系。"
