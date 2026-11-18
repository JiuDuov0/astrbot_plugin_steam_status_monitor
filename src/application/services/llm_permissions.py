"""LLM 工具复用指令的运行时权限和启用状态。"""


def authorize_command(plugin, event, handler_name):
    from astrbot.core.star.filter.permission import PermissionTypeFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    module = type(plugin).__module__
    handlers = [
        handler for handler in star_handlers_registry
        if handler.handler_module_path == module and handler.handler_name == handler_name
    ]
    if len(handlers) != 1 or not handlers[0].enabled:
        return False
    permissions = [
        item for item in handlers[0].event_filters
        if isinstance(item, PermissionTypeFilter)
    ]
    return bool(permissions) and all(item.filter(event, None) for item in permissions)