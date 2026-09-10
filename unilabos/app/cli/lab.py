"""实验室命令模块

提供 lab 子命令：
- lab list: 获取当前用户的所有实验室（GET /lab/list）
"""

import sys

from unilabos.client import (
    EnvelopeError,
    SessionManager,
    print_error,
    print_output,
)

from ._client_factory import make_authenticated_client


def cmd_lab_list(args, session_manager: SessionManager):
    """lab list 命令处理 — GET /lab/list?page=<n>&page_size=<m>"""
    try:
        with session_manager:
            client, _ = make_authenticated_client(args, session_manager)

            try:
                data = client.get(
                    "/lab/list",
                    params={"page": args.page, "page_size": args.page_size},
                )
                print_output(data)
            except EnvelopeError as e:
                print_error(f"获取实验室列表失败: {e.error}")
                sys.exit(1)
            except Exception as e:
                print_error(f"获取实验室列表失败: {e}")
                sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print_error(f"操作失败: {e}")
        sys.exit(1)
