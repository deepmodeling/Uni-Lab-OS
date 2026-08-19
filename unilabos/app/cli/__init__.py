"""Uni-Lab 统一 CLI 命令包。

命令实现、参数注册和分发都收口在此命名空间：
- auth: 认证管理（login, logout, whoami）
- auth_resolver: 凭据多源解析（CLI / session / local_config.py）
- config: 配置管理（config show）
- lab: 实验室管理
- material: 物料管理
- workflow: 工作流管理
- package: 社区设备包 inspect / upload / install
"""

from .auth import cmd_login, cmd_logout, cmd_whoami
from .auth_resolver import resolve_effective_auth
from .config import cmd_config_show
from .lab import cmd_lab_list
from .material import cmd_material_list
from .package import (
    PackageCLIError,
    cmd_package,
    register_package_commands,
    run_package_command,
)
from .workflow import cmd_workflow_upload

__all__ = [
    "cmd_login",
    "cmd_logout",
    "cmd_whoami",
    "resolve_effective_auth",
    "cmd_config_show",
    "cmd_lab_list",
    "cmd_material_list",
    "PackageCLIError",
    "cmd_package",
    "register_package_commands",
    "run_package_command",
    "cmd_workflow_upload",
]
