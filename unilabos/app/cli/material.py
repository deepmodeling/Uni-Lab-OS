"""物料命令模块

提供 material 子命令：
- material list: 查询实验室物料（GET /lab/material?id=<lab_uuid>&with_children=<bool>）
"""

import sys

from unilabos.client import (
    EnvelopeError,
    SessionManager,
    print_error,
    print_output,
)

from ._client_factory import make_authenticated_client


def cmd_material_list(args, session_manager: SessionManager):
    """material list 命令处理 — GET /lab/material?id=<lab_uuid>&with_children=<bool>"""
    try:
        with session_manager:
            client, _ = make_authenticated_client(args, session_manager)

            params = {"id": args.lab_uuid, "with_children": str(args.with_children).lower()}

            try:
                data = client.get("/lab/material", params=params)
                print_output(data)
            except EnvelopeError as e:
                print_error(f"获取物料列表失败: {e.error}")
                sys.exit(1)
            except Exception as e:
                print_error(f"获取物料列表失败: {e}")
                sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print_error(f"操作失败: {e}")
        sys.exit(1)
