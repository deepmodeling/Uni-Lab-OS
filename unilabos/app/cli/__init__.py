"""Uni-Lab 统一 CLI 命令包。

命令实现、参数注册和分发都收口在此命名空间：
- auth: 认证管理（login, logout, whoami）
- auth_resolver: 凭据多源解析（CLI / session / local_config.py）
- config: 配置管理（config show）
- material: 物料管理
- workflow: 工作流管理
- package: 社区设备包 inspect / install
- parser/router: 顶层参数契约与轻量子命令统一分发

子命令实现按需加载：``unilabos.app.main`` 只为构造 argparse 就会 import 本包，
而 material / auth / workflow 会把整套 client + 协议模型（约 1.5s）拖进来。
``from unilabos.app.cli import cmd_login`` 这类写法照常可用，只是推迟到首次访问。
"""

from __future__ import annotations

import importlib
from typing import Any

from .parser import build_parser
from .router import run_cli_command, run_client_command

_LAZY_EXPORTS = {
    "cmd_login": ".auth",
    "cmd_logout": ".auth",
    "cmd_whoami": ".auth",
    "resolve_effective_auth": ".auth_resolver",
    "cmd_config_show": ".config",
    "cmd_material_list": ".material",
    "PackageCLIError": ".package",
    "cmd_package": ".package",
    "register_package_commands": ".package",
    "run_package_command": ".package",
    "cmd_workflow_upload": ".workflow",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name, __name__), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "cmd_login",
    "cmd_logout",
    "cmd_whoami",
    "resolve_effective_auth",
    "cmd_config_show",
    "cmd_material_list",
    "PackageCLIError",
    "cmd_package",
    "register_package_commands",
    "run_package_command",
    "build_parser",
    "run_cli_command",
    "run_client_command",
    "cmd_workflow_upload",
]
