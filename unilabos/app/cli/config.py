"""配置命令模块

提供 config 子命令：
- config show: 显示当前会话配置（base_url, ak/sk 来源, context）
"""

import sys

from unilabos.client import (
    SessionManager,
    print_error,
    print_output,
)


def cmd_config_show(args, session_manager: SessionManager):
    """config show 命令处理 — 显示当前会话配置"""
    from unilabos.app.cli.auth_resolver import resolve_effective_auth

    try:
        with session_manager:
            state = session_manager.get_state()
            effective = resolve_effective_auth(args, session_manager)

            output = {
                "base_url": effective["base_url"],
                "base_url_source": effective["base_url_source"],
                "ak_prefix": effective["ak"][:8] + "..." if effective["ak"] and len(effective["ak"]) > 8 else effective["ak"] or "(未设置)",
                "ak_source": effective["ak_source"],
                "sk_source": effective["sk_source"],
                "context": {
                    "lab_uuid": state.context.lab_uuid or "(未设置)",
                    "project_uuid": state.context.project_uuid or "(未设置)",
                },
            }

            # 如果传了 --address 但与会话中的不同，显示覆盖提示
            addr_override = getattr(args, "address_resolved", None)
            if addr_override is None:
                addr_override = getattr(args, "addr_resolved", None)
            if addr_override and addr_override != state.base_url:
                output["addr_override"] = addr_override

            print_output(output)
    except Exception as e:
        print_error(f"操作失败: {e}")
        sys.exit(1)
