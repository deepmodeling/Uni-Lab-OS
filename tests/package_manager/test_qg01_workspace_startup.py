"""QG01 工作区（Workspace）公共启动合同测试。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


class _RecordingCommunityClient:
    """记录社区包远端解析调用并返回评审场景指定的数据。

    参数：``remote_items`` 是远端端口将返回的社区包解析项目。
    返回：构造后的测试替身会保留每次请求的类名列表。
    异常：无；测试通过调用记录和返回项目表达预期边界。
    """

    def __init__(self, remote_items: list[dict[str, Any]]) -> None:
        """保存远端响应并初始化调用记录。

        参数：``remote_items`` 是每次解析调用返回的社区包项目。
        返回：无；实例随后可模拟 ``resolve_community_packages``。
        异常：无。
        """

        # ``remote_items`` 是评审测试控制的远端社区包响应，不代表可信事实。
        self.remote_items = remote_items
        # ``requested_classes`` 逐次记录真正发送到远端的社区类名。
        self.requested_classes: list[list[str]] = []

    def resolve_community_packages(
        self,
        classes: list[str],
        *,
        current_packages: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """模拟社区包远端解析端口并记录请求。

        参数：``classes`` 是待解析类名；``current_packages`` 是本地缓存指纹。
        返回：包含构造时指定项目的远端响应信封。
        异常：无；冲突响应由被测代码负责失败关闭。
        """

        del current_packages
        self.requested_classes.append(list(classes))
        return {"data": list(self.remote_items)}


def _workspace_api() -> ModuleType:
    """读取本轮工作区（Workspace）公开模块并报告清晰 RED。

    参数：无。
    返回：同时公开 ``WorkspaceSource`` 与 ``prepare_workspace_startup`` 的模块。
    异常：模块或任一公开成员尚未实现时，以测试失败说明缺失合同。
    """

    try:
        workspace_module = importlib.import_module("unilabos.package_manager")
    except ModuleNotFoundError:
        pytest.fail("QG01 缺少 unilabos.package_manager", pytrace=False)
    missing_members = [
        member_name
        for member_name in ("WorkspaceSource", "prepare_workspace_startup")
        if not hasattr(workspace_module, member_name)
    ]
    if missing_members:
        pytest.fail(
            "QG01 缺少工作区公开成员: " + ", ".join(missing_members),
            pytrace=False,
        )
    return workspace_module


def _write_szlab_shaped_workspace(workspace_root: Path) -> dict[str, Path]:
    """创建包含设备、工作流源码和图的最小 SZLab 形状工作区。

    参数：``workspace_root`` 是测试显式授权的工作区根目录。
    返回：按 ``package``、``workflow`` 与 ``graph`` 命名的测试文件路径。
    异常：文件系统写入失败时原样抛出，使测试不能使用不完整夹具。
    """

    # ``workflow_uuid`` 是测试工作流（Workflow）的稳定身份，不代表运行任务。
    workflow_uuid = "5e7ce142-bf5a-5d30-8666-fdf5374941f1"
    # ``package_root`` 是注册表（Registry）将扫描的本地设备包根目录。
    package_root = workspace_root / "szlab_poly_studio"
    # ``workflow_path`` 是工作流源码（Workflow Source）清单授权的真实文件。
    workflow_path = package_root / "workflows" / "material_transfer.py"
    # ``graph_path`` 是公共命令行（CLI）将加载的物理图文件。
    graph_path = workspace_root / "deployment" / "graphs" / "szlab-local-debug.json"
    # ``config_path`` 是工作区启动计划声明的本地部署配置，不属于运行时数据目录。
    config_path = workspace_root / "deployment" / "local_config.py"
    workflow_path.parent.mkdir(parents=True)
    graph_path.parent.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    workflow_path.write_text(
        "from unilabos.workflow.authoring import workflow\n"
        f'@workflow(workflow_uuid="{workflow_uuid}", displayname="物料转移")\n'
        "def material_transfer() -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )
    (workspace_root / "pyproject.toml").write_text(
        '[project]\nname = "szlab-poly-studio"\nversion = "0.1.0"\n'
        "\n[tool.unilabos.startup]\n"
        'graph = "deployment/graphs/szlab-local-debug.json"\n'
        'config = "deployment/local_config.py"\n'
        'app_bridges = ["fastapi"]\n'
        'ensure_dependencies = false\n',
        encoding="utf-8",
    )
    (workspace_root / "package.yaml").write_text(
        "package:\n"
        "  name: szlab_poly_studio\n"
        "workflows:\n"
        f"  - workflow_uuid: {workflow_uuid}\n"
        "    source: szlab_poly_studio/workflows/material_transfer.py\n",
        encoding="utf-8",
    )
    graph_path.write_text(
        '{"nodes":[{"id":"robot","class":'
        '"community.szlab_poly_studio.robot"}],"links":[]}',
        encoding="utf-8",
    )
    config_path.write_text(
        "class BasicConfig:\n    disable_browser = True\n",
        encoding="utf-8",
    )
    return {
        "package": package_root,
        "workflow": workflow_path,
        "graph": graph_path,
        "config": config_path,
    }


def test_workspace_source_reads_only_regular_files_below_explicit_root(
    tmp_path: Path,
) -> None:
    """证明工作区来源只能读取显式根目录内的普通文件。

    参数：``tmp_path`` 隔离合法工作区与越界文件。
    返回：无；断言合法源码可读且父目录逃逸失败关闭。
    异常：若公开实现接受越界路径，测试断言失败。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    source = workspace_api.WorkspaceSource(workspace_root)

    assert source.source_kind == "workspace"
    assert (
        source.read_bytes("package.yaml")
        == (workspace_root / "package.yaml").read_bytes()
    )
    with pytest.raises(ValueError):
        source.read_bytes("../outside.py")
    assert fixture_paths["workflow"].is_file()


def test_prepare_workspace_startup_projects_one_szlab_package_without_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明工作区一次投影为设备扫描、工作流授权和相对图路径。

    参数：``tmp_path`` 创建 SZLab 形状夹具；``monkeypatch`` 隔离 ``sys.path``。
    返回：无；断言只产生启动配置，不创建工作流任务（WorkflowTask）。
    异常：声明身份、目录或图路径无效时实现应抛出稳定 ``ValueError``。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    monkeypatch.setattr(sys, "path", list(sys.path))
    # ``startup_arguments`` 是公共 CLI 完成解析后的启动参数投影。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": "deployment/graphs/szlab-local-debug.json",
        "working_dir": str(tmp_path / "isolated-runtime"),
    }

    startup_plan = workspace_api.prepare_workspace_startup(startup_arguments)

    assert startup_plan is not None
    assert startup_plan.import_package == "szlab_poly_studio"
    assert startup_plan.workflow_source_count == 1
    assert startup_arguments["devices"] == [str(fixture_paths["package"])]
    assert startup_arguments["workflow_editable_package_root"] == [str(workspace_root)]
    assert startup_arguments["_community_namespaces"] == {
        str(fixture_paths["package"]): "community.szlab_poly_studio"
    }
    assert startup_arguments["graph"] == str(fixture_paths["graph"])
    assert startup_arguments["working_dir"] == str(tmp_path / "isolated-runtime")
    assert str(workspace_root) == sys.path[0]
    assert "workflow_task" not in startup_arguments


def test_workspace_startup_defaults_remove_redundant_graph_config_and_bridges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明只给工作区即可补全默认图、配置、桥接器与隔离运行目录。

    参数：``tmp_path`` 创建最小工作区；``monkeypatch`` 隔离 ``sys.path``。
    返回：无；断言工作区声明只填补缺省值，且文件均受工作区边界约束。
    异常：声明缺失、越界或形状无效时准备函数必须失败关闭。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    monkeypatch.setattr(sys, "path", list(sys.path))
    # ``startup_arguments`` 使用公共解析器默认桥接值模拟未显式覆盖的启动命令。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": None,
        "config": None,
        "app_bridges": ["websocket", "fastapi"],
    }

    startup_plan = workspace_api.prepare_workspace_startup(startup_arguments)

    assert startup_plan is not None
    assert startup_arguments["graph"] == str(fixture_paths["graph"])
    assert startup_arguments["config"] == str(fixture_paths["config"])
    assert startup_arguments["app_bridges"] == ["fastapi"]
    assert startup_arguments["working_dir"] == str(workspace_root / ".unilabos")
    assert startup_arguments["_ensure_dependencies"] is False


def test_prepare_workspace_startup_rejects_parallel_legacy_device_roots(
    tmp_path: Path,
) -> None:
    """证明工作区不能与遗留 ``--devices`` 形成第二套包发现权威。

    参数：``tmp_path`` 创建合法工作区和冲突设备目录。
    返回：无；断言冲突在注册表（Registry）扫描前失败关闭。
    异常：若实现未拒绝双发现路径，测试断言失败。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    # ``legacy_device_root`` 代表调用者另行提供的遗留设备扫描权威。
    legacy_device_root = tmp_path / "legacy-devices"
    legacy_device_root.mkdir()
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": [str(legacy_device_root)],
        "workflow_editable_package_root": None,
        "graph": str(fixture_paths["graph"]),
    }

    with pytest.raises(ValueError, match="--workspace.*--devices"):
        workspace_api.prepare_workspace_startup(startup_arguments)


def test_public_cli_parser_accepts_workspace_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明公共 ``unilab`` 解析器接受显式 ``--workspace``。

    参数：``tmp_path`` 提供不需要实际启动的参数值；``monkeypatch`` 隔离进程参数。
    返回：无；断言解析结果保留调用者声明的工作区路径。
    异常：未知参数应由 argparse 触发 ``SystemExit``，本测试会因此失败。
    """

    from unilabos.app.main import parse_args

    workspace_root = tmp_path / "workspace"
    monkeypatch.setattr(
        sys,
        "argv",
        ["unilab", "--workspace", str(workspace_root)],
    )

    parsed_arguments = parse_args().parse_args()

    assert parsed_arguments.workspace == str(workspace_root)


def test_workspace_dependency_policy_rejects_non_boolean_value(
    tmp_path: Path,
) -> None:
    """证明工作区依赖保障策略必须是明确布尔值。

    参数：``tmp_path`` 创建损坏的工作区声明。返回：无；断言配置在环境检查前
    失败关闭。异常：若字符串被隐式当作布尔值，测试断言失败。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    _write_szlab_shaped_workspace(workspace_root)
    pyproject_path = workspace_root / "pyproject.toml"
    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            "ensure_dependencies = false",
            'ensure_dependencies = "false"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ensure_dependencies"):
        workspace_api.prepare_workspace_startup(
            {
                "workspace": str(workspace_root),
                "devices": None,
                "graph": None,
                "config": None,
                "app_bridges": ["websocket", "fastapi"],
            }
        )


def test_workspace_dependency_policy_defaults_to_ensure(
    tmp_path: Path,
) -> None:
    """证明未声明依赖策略的工作区仍执行检查和自动补齐。

    参数：``tmp_path`` 创建省略策略的合法工作区。返回：无；断言启动计划使用
    安全默认值。异常：若默认值退回跳过，测试断言失败。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    _write_szlab_shaped_workspace(workspace_root)
    pyproject_path = workspace_root / "pyproject.toml"
    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            "ensure_dependencies = false\n",
            "",
        ),
        encoding="utf-8",
    )
    arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "graph": None,
        "config": None,
        "app_bridges": ["websocket", "fastapi"],
    }

    workspace_api.prepare_workspace_startup(arguments)

    assert arguments["_ensure_dependencies"] is True


def test_local_workspace_namespace_satisfies_community_graph_without_remote_package(
    tmp_path: Path,
) -> None:
    """证明 SZLab 本地命名空间阻止工作区图误走远端社区包解析。

    参数：``tmp_path`` 提供隔离工作目录和本地包目录。
    返回：无；断言已提供命名空间原样进入注册表（Registry）扫描映射。
    异常：若本地命名空间仍被判断为缺失，准备函数会抛出社区包错误。
    """

    from unilabos.app.community_packages import prepare_community_packages

    package_root = tmp_path / "szlab_poly_studio"
    package_root.mkdir()
    # ``local_namespaces`` 是设备扫描目录到社区类名前缀的唯一显式映射。
    local_namespaces = {str(package_root.resolve()): "community.szlab_poly_studio"}
    graph_document = {
        "nodes": [
            {
                "id": "robot",
                "class": "community.szlab_poly_studio.robot",
            }
        ],
        "links": [],
    }

    result = prepare_community_packages(
        graph_document,
        working_dir=tmp_path,
        available_namespaces=local_namespaces,
    )

    assert result.namespaces == local_namespaces
    assert result.devices_dirs == []


def test_local_workspace_namespace_is_filtered_before_remote_resolution(
    tmp_path: Path,
) -> None:
    """证明本地工作区命名空间不会发送到社区包远端解析端口。

    参数：``tmp_path`` 提供隔离工作目录和本地包目录。
    返回：无；断言远端端口未被调用且本地目录保持唯一发现权威。
    异常：若实现仍把本地类名发往远端，调用记录断言失败。
    """

    from unilabos.app.community_packages import prepare_community_packages

    # ``package_root`` 是当前启动已授权的本地设备包目录。
    package_root = tmp_path / "szlab_poly_studio"
    package_root.mkdir()
    # ``local_namespaces`` 固定本地目录对社区命名空间的唯一映射。
    local_namespaces = {str(package_root.resolve()): "community.szlab_poly_studio"}
    # ``remote_client`` 会记录任何不应发生的远端解析调用。
    remote_client = _RecordingCommunityClient([])
    # ``graph_document`` 只引用已由工作区提供的社区设备类。
    graph_document = {
        "nodes": [
            {
                "id": "robot",
                "class": "community.szlab_poly_studio.robot",
            }
        ],
        "links": [],
    }

    result = prepare_community_packages(
        graph_document,
        working_dir=tmp_path,
        http_client=remote_client,
        available_namespaces=local_namespaces,
    )

    assert remote_client.requested_classes == []
    assert result.namespaces == local_namespaces
    assert result.devices_dirs == []


def test_remote_response_cannot_override_local_workspace_namespace(
    tmp_path: Path,
) -> None:
    """证明远端返回本地同名命名空间时启动失败关闭。

    参数：``tmp_path`` 提供隔离工作目录和本地包目录。
    返回：无；断言远端仅收到缺失类且同名响应不能覆盖本地工作区。
    异常：预期抛出 ``CommunityPackageError``；未抛出即表示发现权威冲突。
    """

    from unilabos.app.community_packages import (
        CommunityPackageError,
        prepare_community_packages,
    )

    # ``package_root`` 是已被启动计划授权的本地设备包目录。
    package_root = tmp_path / "szlab_poly_studio"
    package_root.mkdir()
    # ``local_namespaces`` 声明不得被远端结果替换的本地命名空间。
    local_namespaces = {str(package_root.resolve()): "community.szlab_poly_studio"}
    # ``remote_client`` 故意在解析另一包时返回与本地工作区同名的冲突项目。
    remote_client = _RecordingCommunityClient(
        [{"class_namespace": "community.szlab_poly_studio"}]
    )
    # ``graph_document`` 同时引用本地类与确实需要远端解析的类。
    graph_document = {
        "nodes": [
            {
                "id": "robot",
                "class": "community.szlab_poly_studio.robot",
            },
            {
                "id": "remote-reader",
                "class": "community.remote_reader.reader",
            },
        ],
        "links": [],
    }

    with pytest.raises(CommunityPackageError, match="本地工作区.*命名空间"):
        prepare_community_packages(
            graph_document,
            working_dir=tmp_path,
            http_client=remote_client,
            available_namespaces=local_namespaces,
        )

    assert remote_client.requested_classes == [["community.remote_reader.reader"]]


def test_prepare_workspace_startup_rejects_absolute_graph_outside_workspace(
    tmp_path: Path,
) -> None:
    """证明绝对物理图路径也必须位于显式工作区内。

    参数：``tmp_path`` 隔离合法工作区和越界物理图文件。
    返回：无；断言越界绝对路径在注册表（Registry）扫描前失败关闭。
    异常：预期 ``ValueError``；接受越界路径会使测试失败。
    """

    workspace_api = _workspace_api()
    # ``workspace_root`` 是公共命令行（CLI）显式授权的唯一根目录。
    workspace_root = tmp_path / "workspace"
    _write_szlab_shaped_workspace(workspace_root)
    # ``outside_graph`` 是存在但未被工作区授权的绝对物理图文件。
    outside_graph = tmp_path / "outside-graph.json"
    outside_graph.write_text('{"nodes":[],"links":[]}', encoding="utf-8")
    # ``startup_arguments`` 模拟公共命令行完成解析后的启动参数。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": str(outside_graph),
    }

    with pytest.raises(ValueError, match="工作区"):
        workspace_api.prepare_workspace_startup(startup_arguments)


def test_prepare_workspace_startup_rejects_absolute_graph_symlink(
    tmp_path: Path,
) -> None:
    """证明工作区内的绝对物理图路径不得经过符号链接。

    参数：``tmp_path`` 创建真实物理图和同目录符号链接。
    返回：无；断言符号链接不能成为公共命令行（CLI）的图输入。
    异常：预期 ``ValueError``；接受符号链接会使测试失败。
    """

    workspace_api = _workspace_api()
    # ``workspace_root`` 是启动计划唯一授权的工作区目录。
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    # ``graph_symlink`` 位于工作区内，但路径身份经过符号链接。
    graph_symlink = fixture_paths["graph"].with_name("linked-graph.json")
    graph_symlink.symlink_to(fixture_paths["graph"])
    # ``startup_arguments`` 模拟携带绝对符号链接图路径的公共命令行结果。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": str(graph_symlink),
    }

    with pytest.raises(ValueError, match="符号链接"):
        workspace_api.prepare_workspace_startup(startup_arguments)


def test_prepare_workspace_startup_rejects_registry_python_symlink_escape(
    tmp_path: Path,
) -> None:
    """证明注册表扫描包内的 Python 文件不得通过符号链接越界。

    参数：``tmp_path`` 隔离合法工作区与越界 Python 文件。
    返回：无；断言启动编译在注册表（Registry）读取越界源码前失败关闭。
    异常：预期 ``ValueError``；允许符号链接源码会使测试失败。
    """

    workspace_api = _workspace_api()
    # ``workspace_root`` 是公共命令行（CLI）显式授权的唯一根目录。
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    # ``outside_python`` 模拟攻击者希望注册表（Registry）扫描的越界源码。
    outside_python = tmp_path / "outside_device.py"
    outside_python.write_text("UNTRUSTED = True\n", encoding="utf-8")
    # ``escaped_python`` 是包目录内指向越界源码的符号链接。
    escaped_python = fixture_paths["package"] / "escaped_device.py"
    escaped_python.symlink_to(outside_python)
    # ``startup_arguments`` 模拟公共命令行完成解析后的启动参数。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": str(fixture_paths["graph"]),
    }

    with pytest.raises(ValueError, match="Python.*符号链接"):
        workspace_api.prepare_workspace_startup(startup_arguments)
