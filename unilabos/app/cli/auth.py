"""认证命令模块

基于 ak/sk 的认证：
- login: 保存 ak/sk 到会话文件
- logout: 清除本地 ak/sk
- whoami: 显示当前有效的 ak/sk 来源（CLI / session.json / local_config.py）

由于后端没有 whoami 端点，验证发生在实际 API 调用时（如 lab list）返回 401。
"""

import sys

from unilabos.client import (
    SessionManager,
    print_error,
    print_output,
    print_success,
)


class AuthError(Exception):
    """认证错误"""
    pass


def cmd_login(args, session_manager: SessionManager):
    """login 命令处理 — 保存 ak/sk 到会话文件

    如果同时传了 --addr，会一并持久化 base_url。
    """
    try:
        with session_manager:
            state = session_manager.get_state()

            addr = getattr(args, "addr_resolved", None)
            if addr:
                state.base_url = addr

            state.auth.ak = args.ak
            state.auth.sk = args.sk

            print_success(f"已保存 ak/sk 到 {session_manager.session_file}")
            print_output({
                "base_url": state.base_url,
                "ak_prefix": args.ak[:8] + "..." if len(args.ak) > 8 else args.ak,
            })
    except Exception as e:
        print_error(f"登录失败: {e}")
        sys.exit(1)


def cmd_logout(args, session_manager: SessionManager):
    """logout 命令处理 — 清除本地 ak/sk"""
    try:
        with session_manager:
            state = session_manager.get_state()
            state.auth.ak = ""
            state.auth.sk = ""
            state.auth.user_name = ""
            print_success("已登出")
    except Exception as e:
        print_error(f"登出失败: {e}")
        sys.exit(1)


def cmd_whoami(args, session_manager: SessionManager):
    """whoami 命令处理 — 显示当前有效的 ak/sk 来源

    优先级：CLI 参数 > session.json > local_config.py 中的 BasicConfig.ak/sk
    """
    from unilabos.app.cli.auth_resolver import resolve_effective_auth

    try:
        with session_manager:
            effective = resolve_effective_auth(args, session_manager)

            if not effective["ak"] or not effective["sk"]:
                print_error(
                    "未找到 ak/sk。请通过以下方式之一配置：\n"
                    "  1. unilab login --ak <ak> --sk <sk>\n"
                    "  2. 命令行传入 --ak <ak> --sk <sk>\n"
                    "  3. 在 local_config.py 中设置 BasicConfig.ak/sk"
                )
                sys.exit(1)

            print_output({
                "ak_prefix": effective["ak"][:8] + "..." if len(effective["ak"]) > 8 else effective["ak"],
                "ak_source": effective["ak_source"],
                "sk_source": effective["sk_source"],
                "base_url": effective["base_url"],
                "base_url_source": effective["base_url_source"],
            })
    except Exception as e:
        print_error(f"操作失败: {e}")
        sys.exit(1)
