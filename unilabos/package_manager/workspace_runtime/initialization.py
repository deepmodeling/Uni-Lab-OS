"""创建可由 Uni-Lab OS 直接读取的新工作区设备包骨架。"""

from __future__ import annotations

import json
import keyword
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

_PACKAGE_IDENTITY_PATTERN = re.compile(r"^[a-z](?:[a-z0-9._-]*[a-z0-9])?$")
_SCHEMA_VERSION = "unilab-workspace-init/v1"


class WorkspaceInitError(Exception):
    """工作区初始化失败，并携带可供 CLI 稳定输出的错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        """返回不包含本地文件内容的公开错误合同。"""

        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class InitializedWorkspace:
    """一次成功初始化产生的工作区身份与文件清单。"""

    root: Path
    distribution_name: str
    import_package: str
    created_files: tuple[str, ...]

    @property
    def next_commands(self) -> tuple[str, ...]:
        """返回从需求确认到包检查的最短安全命令链。"""

        return (
            f"cd {shlex.quote(str(self.root))}",
            "python -m pip install -e '.[dev]'",
            "python -m pytest -q",
            "unilab package inspect --path . --out dist/inspect",
        )

    def as_dict(self) -> dict[str, Any]:
        """投影为 CLI 使用的稳定 JSON 结果。"""

        return {
            "ok": True,
            "schemaVersion": _SCHEMA_VERSION,
            "workspacePath": str(self.root),
            "distributionName": self.distribution_name,
            "importPackage": self.import_package,
            "createdFiles": list(self.created_files),
            "nextCommands": list(self.next_commands),
        }

    def render_text(self) -> str:
        """生成人员可直接照做的成功提示，不暴露 JSON 内部字段。"""

        commands = "\n".join(f"  {command}" for command in self.next_commands)
        return (
            f"工作区已创建：{self.root}\n"
            f"发布名称：{self.distribution_name}\n"
            f"Python 包：{self.import_package}\n"
            "先填写：DEVICE_PACKAGE_REQUIREMENTS.md\n"
            f"下一步：\n{commands}\n"
        )


def initialize_workspace(
    output: str | Path,
    *,
    package_name: str | None = None,
) -> InitializedWorkspace:
    """在一个尚不存在的目标目录中创建设备包骨架。

    参数：``output`` 是待创建的工作区目录；``package_name`` 是可选 Python 包名，
    省略时从输出目录名规范化得到。返回：创建完成的身份和文件清单。
    异常：包名非法、目标已存在或文件系统写入失败时抛出 ``WorkspaceInitError``；
    任何失败都不会覆盖既有目录，写入中途失败会清理本次创建的目标。
    """

    root = _resolve_output(output)
    import_package = _resolve_package_name(root, package_name)
    distribution_name = import_package.replace("_", "-")
    if root.exists() or root.is_symlink():
        raise WorkspaceInitError(
            "workspace_exists",
            f"目标目录已存在，未做任何修改: {root}",
        )

    files = _workspace_files(import_package, distribution_name)
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise WorkspaceInitError(
            "workspace_init_failed",
            f"无法准备工作区父目录: {root.parent}",
        ) from error
    try:
        # 在最终目标上使用排他创建，保证并发创建也不能覆盖已有目录。
        root.mkdir()
    except FileExistsError as error:
        raise WorkspaceInitError(
            "workspace_exists",
            f"目标目录已存在，未做任何修改: {root}",
        ) from error
    except OSError as error:
        raise WorkspaceInitError(
            "workspace_init_failed",
            f"无法创建工作区目录: {root}",
        ) from error
    try:
        root.joinpath("tests").mkdir()
        for relative_path, content in files.items():
            target = root.joinpath(*Path(relative_path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    except OSError as error:
        if root.is_dir() and not root.is_symlink():
            shutil.rmtree(root)
        raise WorkspaceInitError(
            "workspace_init_failed",
            f"创建工作区失败: {root}",
        ) from error

    return InitializedWorkspace(
        root=root,
        distribution_name=distribution_name,
        import_package=import_package,
        created_files=tuple(sorted(files)),
    )


def _resolve_output(output: str | Path) -> Path:
    """把 CLI 路径解析为不要求预先存在的绝对目标。"""

    if not isinstance(output, (str, Path)) or not str(output).strip():
        raise WorkspaceInitError("invalid_output", "--output 必须是非空目录路径")
    requested = Path(output).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    # ``abspath`` 只折叠相对段，不追随末端符号链接；已存在或悬空链接必须由
    # 调用方按“目标已存在”拒绝，不能在链接指向位置意外创建工作区。
    return Path(os.path.abspath(requested))


def _resolve_package_name(root: Path, explicit_name: str | None) -> str:
    """确定唯一 Python 包身份，并执行文档声明的 ASCII 命名约束。"""

    if explicit_name is None:
        identity = root.name.lower()
    elif isinstance(explicit_name, str):
        identity = explicit_name.strip()
    else:
        identity = ""
    if not _PACKAGE_IDENTITY_PATTERN.fullmatch(identity):
        raise WorkspaceInitError(
            "invalid_package_name",
            "包名必须以小写英文字母开头、以字母或数字结尾，"
            "且只能包含小写字母、数字、点、连字符和下划线",
        )
    # 与包目录编译器的发行名称规范化一致，连续混合分隔符折叠为一个下划线。
    import_package = re.sub(r"[-_.]+", "_", identity)
    if not import_package.isidentifier() or keyword.iskeyword(import_package):
        raise WorkspaceInitError(
            "invalid_package_name",
            f"规范化后的包名必须是有效的 Python 标识符，且不能是关键字: {import_package}",
        )
    return import_package


def _workspace_files(
    import_package: str,
    distribution_name: str,
) -> dict[str, str]:
    """生成与产品说明书一致、可直接学习和验证的设备包文件集合。"""

    demo_workflow_uuid = _demo_uuid(import_package, "workflow")
    demo_node_uuid = _demo_uuid(import_package, "workflow-node")
    demo_device_uuid = _demo_uuid(import_package, "device-instance")

    pyproject = f'''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{distribution_name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["unilabos"]

[project.optional-dependencies]
dev = ["build>=1.2", "pytest>=8"]

[tool.setuptools.packages.find]
include = ["{import_package}*"]

[tool.unilabos.startup]
graph = "deployment/graphs/dry-run.json"
config = "deployment/local_config.py"
ensure_dependencies = true
'''
    package_manifest = f'''package:
  name: {import_package}

workflows:
  - workflow_uuid: {demo_workflow_uuid}
    source: {import_package}/workflows/demo_workflow.py
'''
    local_config = '''class BasicConfig:
    disable_browser = True
    no_update_feedback = True
    log_level = "INFO"
'''
    graph = json.dumps(
        {
            "nodes": [
                {
                    "id": "demo_device_01",
                    "uuid": demo_device_uuid,
                    "name": "示例设备",
                    "class": f"community.{import_package}.demo_device",
                    "type": "device",
                    "config": {},
                    "data": {},
                }
            ],
            "links": [],
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    readme = f'''# {distribution_name}

Uni-Lab OS 设备包工作区。

开始开发前先填写 [设备包需求卡](DEVICE_PACKAGE_REQUIREMENTS.md)，不要猜测设备动作、
参数单位、连接地址或安全联锁。

## 交付信息

- 用途：待填写
- 负责人：待填写
- 适用的 Uni-Lab OS 版本：待填写

生产地址、账号和密钥不得写入源码或启动图。

## 自带示例

- `{import_package}/devices/demo_device.py`：无硬件副作用的示例驱动，展示设备和动作定义；
- `{import_package}/workflows/demo_workflow.py`：调用示例设备动作的最小工作流；
- `deployment/graphs/dry-run.json`：只装载示例设备，不包含真实连接参数。

先运行测试和 `package inspect` 确认示例可用，再以相同结构替换为真实设备合同。
不要把示例动作的成功结果当成真实设备验收结果。
'''
    requirements = f'''# 设备包需求卡

- 发布名称：`{distribution_name}`
- Python 包名：`{import_package}`
- 负责人：待确认
- 初始用途：待确认

## 设备

- 名称与型号：待确认
- 控制方式（PLC／串口／TCP／HTTP／SDK）：待确认
- 动作、参数、单位与范围：待确认
- 成功、失败、超时和取消语义：待确认
- 状态、报警、急停与安全联锁：待确认
- 协议或供应商资料位置：待确认

## 物料与位置

- 物料模板及规格：待确认
- 工作站、仓库、Stack 与 Site：待确认
- 条码、数量、占用和兼容规则：待确认

## 工作流与验收

- 目标工作流及输入输出：待确认
- 模拟器或 dry-run 范围：待确认
- 真机验收负责人和完成标准：待确认
- 禁止事项（真机连接、发布、凭证写入等）：待确认

未知信息保持“待确认”。不要从其他项目复制设备实例 ID、地址、UUID 或安全阈值。
初始化生成的 `demo_device.py` 和 `demo_workflow.py` 只用于教学，不能代替上述事实确认。
'''
    demo_device = '''"""无硬件副作用的教学驱动；接入真实设备时请替换本文件。"""

from typing import TypedDict

from unilabos.registry.decorators import action, device


class EchoResult(TypedDict):
    success: bool
    echoed_text: str


@device(
    id="demo_device",
    category=["example"],
    displayname="示例设备",
    description="回显文本的无硬件副作用教学设备。",
    version="0.1.0",
    metadata={"hardware_side_effects": False},
)
class DemoDevice:
    """演示设备定义与动作实现之间的最小接口。"""

    @action(
        displayname="回显文本",
        description="原样返回输入文本，用于验证设备包调用链。",
        estimate_duration_fixed=1.0,
    )
    def echo(self, text: str) -> EchoResult:
        """回显输入文本，不连接或控制任何硬件。"""

        return {"success": True, "echoed_text": text}
'''
    demo_workflow = f'''"""调用示例设备动作的最小 Uni-Lab OS 工作流。"""

from typing import TypedDict

from {import_package}.devices.demo_device import DemoDevice
from unilabos.workflow.authoring import device, workflow


class DemoWorkflowResult(TypedDict):
    success: bool
    echoed_text: str


demo_device: DemoDevice = device("demo_device_01")


@workflow(
    workflow_uuid="{demo_workflow_uuid}",
    displayname="示例设备回显",
    description="调用示例设备动作并返回结果。",
)
def run_demo(*, text: str = "Hello Uni-Lab OS") -> DemoWorkflowResult:
    # unilab:node_uuid={demo_node_uuid}
    echoed = demo_device.echo(text=text)
    return {{
        "success": echoed.success,
        "echoed_text": echoed.echoed_text,
    }}
'''
    contract_test = f'''"""生成工作区的包身份、启动文件和教学示例合同。"""

import json
from pathlib import Path

from {import_package}.devices.demo_device import DemoDevice
from unilabos.package_manager import (
    WorkspaceSource,
    compile_package_source,
    compile_workspace_startup,
)


WORKSPACE = Path(__file__).parents[1]


def test_workspace_identity_and_startup_files() -> None:
    source = WorkspaceSource(WORKSPACE)
    plan = compile_workspace_startup(source)
    assert plan.distribution_name == "{distribution_name}"
    assert plan.import_package == "{import_package}"
    assert plan.workflow_source_count == 1

    graph = json.loads(plan.resolve_graph(plan.default_graph).read_text(encoding="utf-8"))
    assert graph["nodes"][0]["id"] == "demo_device_01"
    assert graph["nodes"][0]["class"] == "community.{import_package}.demo_device"
    assert graph["links"] == []

    catalog = compile_package_source(source)
    assert [item.id for item in catalog.definitions.devices] == ["demo_device"]
    assert [item.id for item in catalog.definitions.workflows] == ["run_demo"]


def test_demo_driver_echoes_text_without_hardware() -> None:
    assert DemoDevice().echo(text="hello") == {{
        "success": True,
        "echoed_text": "hello",
    }}
'''
    package_root = import_package
    return {
        ".gitignore": (
            ".unilabos/\ndist/\n*.egg-info/\n__pycache__/\n.pytest_cache/\n.DS_Store\n"
        ),
        "DEVICE_PACKAGE_REQUIREMENTS.md": requirements,
        "README.md": readme,
        "deployment/graphs/dry-run.json": graph,
        "deployment/local_config.py": local_config,
        "package.yaml": package_manifest,
        "pyproject.toml": pyproject,
        f"{package_root}/__init__.py": "",
        f"{package_root}/devices/__init__.py": "",
        f"{package_root}/devices/demo_device.py": demo_device,
        f"{package_root}/experiment_operations/__init__.py": "",
        f"{package_root}/resources/__init__.py": "",
        f"{package_root}/workflows/__init__.py": "",
        f"{package_root}/workflows/demo_workflow.py": demo_workflow,
        "tests/test_workspace_contract.py": contract_test,
    }


def _demo_uuid(import_package: str, role: str) -> str:
    """为生成示例派生可重现且不会跨包共享的稳定 UUID 字面量。"""

    return str(uuid5(NAMESPACE_URL, f"urn:unilabos:{import_package}:demo:{role}"))


__all__ = [
    "InitializedWorkspace",
    "WorkspaceInitError",
    "initialize_workspace",
]
