"""工作区启动与物料布局使用设备包声明的默认图。"""

import json
from pathlib import Path

import pytest

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.launch import resolve_backend_launch
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths


@pytest.fixture
def paths(tmp_path: Path) -> WorkspacePaths:
    """建立声明默认图的最小工作区，不启动设备进程。"""
    (tmp_path / "deployment/graphs").mkdir(parents=True)
    for name in ("dry-run", "override", "environment"):
        (tmp_path / f"deployment/graphs/{name}.json").write_text("{}")
    (tmp_path / "deployment/local_config.py").write_text("# 测试配置\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo-lab"\n'
        '[tool.unilabos.startup]\ngraph = "deployment/graphs/dry-run.json"\n'
    )
    result = WorkspacePaths.resolve(tmp_path)
    result.prepare()
    ensure_local_token(result)
    return result


@pytest.mark.parametrize(
    "explicit,environment,expected",
    [
        (None, None, "dry-run"),
        (None, "environment", "environment"),
        ("override", "environment", "override"),
    ],
)
def test_launch_graph_precedence(
    paths: WorkspacePaths,
    explicit: str | None,
    environment: str | None,
    expected: str,
) -> None:
    """命令行优先于本地环境，本地环境优先于设备包声明。"""
    if environment:
        paths.environment.write_text(json.dumps({
            "schemaVersion": 1,
            "graphPath": f"deployment/graphs/{environment}.json",
        }))
    plan = resolve_backend_launch(
        paths, graph_path=f"deployment/graphs/{explicit}.json" if explicit else None,
        backend_port=48101,
    )
    assert plan.metadata["graphPath"] == str(
        paths.workspace / f"deployment/graphs/{expected}.json"
    )
    frozen = Path(plan.command[plan.command.index("--graph") + 1])
    assert frozen.name == f"{expected}.json"


@pytest.mark.parametrize("declaration,code", [
    ('[project]\nname="demo-lab"\n', "graph_not_configured"),
    ('invalid toml [', "workspace_project_invalid"),
    ('[project]\nname="demo-lab"\n[tool.unilabos.startup]\ngraph="missing.json"\n', "graph_not_found"),
])
def test_bad_default_does_not_guess_graph(
    paths: WorkspacePaths, declaration: str, code: str
) -> None:
    """未配置、配置损坏或文件缺失时明确报错，不猜测 SZLab 图。"""
    (paths.workspace / "pyproject.toml").write_text(declaration)
    with pytest.raises(WorkspaceHostError) as caught:
        resolve_backend_launch(paths)
    assert caught.value.code == code


def test_material_layout_uses_project_default(paths: WorkspacePaths) -> None:
    """未启动 OS 时，布局入口也采用同一设备包默认图。"""
    host = WorkspaceHost(paths, ensure_local_token(paths))
    try:
        assert host._material_layout().graph_path == (
            paths.workspace / "deployment/graphs/dry-run.json"
        )
    finally:
        host.close()


def test_initialized_workspace_launches_with_declared_graph(tmp_path: Path) -> None:
    """init 产物无需重复指定 graph 就能生成启动计划。"""
    from unilabos.package_manager import initialize_workspace

    workspace = tmp_path / "sample-lab"
    initialize_workspace(workspace)
    paths = WorkspacePaths.resolve(workspace)
    ensure_local_token(paths)
    plan = resolve_backend_launch(paths, backend_port=48101)
    assert plan.metadata["graphPath"] == str(workspace / "deployment/graphs/dry-run.json")


def test_missing_project_requires_explicit_graph(paths: WorkspacePaths) -> None:
    """旧工作区未声明默认图时仍可显式选图。"""
    (paths.workspace / "pyproject.toml").unlink()
    with pytest.raises(WorkspaceHostError) as caught:
        resolve_backend_launch(paths)
    assert caught.value.code == "graph_not_configured"
    plan = resolve_backend_launch(
        paths, graph_path="deployment/graphs/override.json",
        backend_port=48101,
    )
    assert plan.metadata["graphPath"] == str(paths.workspace / "deployment/graphs/override.json")
