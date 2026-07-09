"""工作流命令模块

提供 workflow 子命令：
- workflow upload: 上传工作流文件（迁移自 workflow_upload）

通过 resolve_effective_auth 注入凭据到 BasicConfig / HTTPConfig，
然后委托给现有的 handle_workflow_upload_command 实现。
"""

import sys
from typing import Any, Dict

from unilabos.client import (
    SessionManager,
    print_error,
    print_success,
)


def _inject_credentials(args: Any, session_manager: SessionManager) -> bool:
    """将解析后的 ak/sk + base_url 注入到 BasicConfig / HTTPConfig

    Returns:
        是否成功注入（凭据完整时返回 True）
    """
    from unilabos.app.cli.auth_resolver import resolve_effective_auth
    from unilabos.config.config import BasicConfig, HTTPConfig

    effective = resolve_effective_auth(args, session_manager)

    if not effective["ak"] or not effective["sk"]:
        print_error(
            "未找到 ak/sk。请通过以下方式之一配置：\n"
            "  1. unilab login --ak <ak> --sk <sk>\n"
            "  2. 命令行传入 --ak <ak> --sk <sk>\n"
            "  3. 在 local_config.py 中设置 BasicConfig.ak/sk"
        )
        return False

    BasicConfig.ak = effective["ak"]
    BasicConfig.sk = effective["sk"]
    BasicConfig.working_dir = str(session_manager.working_dir)
    HTTPConfig.remote_addr = effective["base_url"]
    return True


def cmd_workflow_upload(args, session_manager: SessionManager):
    """workflow upload 命令处理"""
    try:
        with session_manager:
            if not _inject_credentials(args, session_manager):
                sys.exit(1)

        # 注意：handle_workflow_upload_command 期待 args_dict 形式
        from unilabos.workflow.wf_utils import handle_workflow_upload_command

        args_dict: Dict[str, Any] = {
            "workflow_file": args.workflow_file,
            "workflow_name": args.workflow_name,
            "tags": args.tags or [],
            "published": args.published,
            "description": args.description or "",
        }
        handle_workflow_upload_command(args_dict)
        print_success("工作流上传完成")
    except SystemExit:
        raise
    except Exception as e:
        print_error(f"上传失败: {e}")
        sys.exit(1)
