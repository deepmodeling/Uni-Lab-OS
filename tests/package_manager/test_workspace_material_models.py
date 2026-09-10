"""工作区（Workspace）3D 模型目录与受限资产读取合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.package_manager import (
    WorkspaceSource,
    compile_package_source,
    compile_workspace_material_models,
    compile_workspace_startup,
)


class _Registry:
    """提供单个工作区资源模板模型声明的注册表（Registry）替身。"""

    def __init__(self, definition: dict[str, Any]) -> None:
        """保存模型声明。

        参数：``definition`` 是已由注册表（Registry）发现的资源模板定义。
        返回：无。异常：无。
        """

        self._definition = definition

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回空设备定义。参数：无。返回：空列表。异常：无。"""

        return []

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回模型资源定义。参数：无。返回：单元素列表。异常：无。"""

        return [dict(self._definition)]


def _workspace(root: Path) -> tuple[Any, Path]:
    """建立包含 Xacro 与 STL 的最小显式工作区。

    参数：``root`` 是测试授权根。返回：启动计划与装饰器声明文件。
    异常：文件写入失败时原样抛出。
    """

    package = root / "szlab_poly_studio"
    declaration = package / "resources" / "beaker.py"
    model_root = package / "resources" / "beaker" / "models"
    mesh = model_root / "meshes" / "beaker.stl"
    mesh.parent.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    declaration.write_text(
        """from unilabos.registry.decorators import resource


@resource(
    id="szlab_beaker_500ml",
    model={
        "format": "xacro",
        "entry": "beaker/models/resource.xacro",
        "macro": "szlab_beaker_500ml",
    },
)
def szlab_beaker_500ml(name: str = "beaker"):
    return object()
""",
        encoding="utf-8",
    )
    (model_root / "resource.xacro").write_text(
        '<robot><mesh filename="file://${mesh_path}/meshes/beaker.stl"/></robot>',
        encoding="utf-8",
    )
    mesh.write_bytes(b"solid beaker\nendsolid beaker\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "szlab-poly-studio"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    plan = compile_workspace_startup(WorkspaceSource(root))
    return plan, declaration


def test_workspace_model_catalog_projects_binding_and_serves_related_assets(
    tmp_path: Path,
) -> None:
    """模型声明必须投影公共 URL，并只读取声明模型目录内的真实资产。

    参数：``tmp_path`` 隔离模型工作区。返回：无；断言模型快照和两种资产。
    异常：目录丢失、媒体类型错误或资产读取失败时测试失败。
    """

    plan, declaration = _workspace(tmp_path / "workspace")
    catalog = compile_workspace_material_models(
        plan,
        _Registry(
            {
                "id": "community.szlab_poly_studio.szlab_beaker_500ml",
                "file_path": str(declaration),
                "model": {
                    "format": "xacro",
                    "entry": "beaker/models/resource.xacro",
                    "macro": "szlab_beaker_500ml",
                },
            }
        ),
    )

    public_entry = (
        "/api/v1/material-models/szlab-poly-studio/"
        "szlab_poly_studio/resources/beaker/models/resource.xacro"
    )
    model = catalog.models_by_template["community.szlab_poly_studio.szlab_beaker_500ml"]
    assert model == {
        "path": public_entry,
        "format": "xacro",
        "meshDir": public_entry.rsplit("/", 1)[0],
        "macro": "szlab_beaker_500ml",
    }
    entry = catalog.read_asset(public_entry)
    mesh = catalog.read_asset(public_entry.rsplit("/", 1)[0] + "/meshes/beaker.stl")
    assert entry.content.startswith(b"<robot>")
    assert entry.media_type == "application/xml"
    assert mesh.content.startswith(b"solid beaker")
    assert mesh.media_type == "model/stl"
    assert entry.etag.startswith("sha256:")


def test_workspace_model_catalog_compiles_package_catalog_definitions(
    tmp_path: Path,
) -> None:
    """模型目录必须直接消费工作区同代包目录，而不依赖注册表绝对路径。

    参数：``tmp_path`` 隔离工作区。返回：无；断言不可变包目录仍能发布模型。
    异常：模型编译退回旧注册表 ``file_path`` 合同时测试失败。
    """

    plan, _ = _workspace(tmp_path / "workspace")
    package_catalog = compile_package_source(plan.source, startup_plan=plan)

    catalog = compile_workspace_material_models(plan, package_catalog)

    model = catalog.models_by_template[
        "community.szlab_poly_studio.szlab_beaker_500ml"
    ]
    assert model["path"] == (
        "/api/v1/material-models/szlab-poly-studio/"
        "szlab_poly_studio/resources/beaker/models/resource.xacro"
    )
    assert catalog.read_asset(model["path"]).content.startswith(b"<robot>")


def test_workspace_model_catalog_rejects_assets_outside_declared_model_root(
    tmp_path: Path,
) -> None:
    """公共模型读取不得越过装饰器声明的模型目录。

    参数：``tmp_path`` 隔离工作区。返回：无；断言未授权路径关闭失败。
    异常：实现错误接受越界路径时测试失败。
    """

    plan, declaration = _workspace(tmp_path / "workspace")
    catalog = compile_workspace_material_models(
        plan,
        _Registry(
            {
                "id": "beaker",
                "file_path": str(declaration),
                "model": {
                    "format": "xacro",
                    "entry": "beaker/models/resource.xacro",
                },
            }
        ),
    )

    with pytest.raises(KeyError, match="模型资产未授权"):
        catalog.read_asset("/api/v1/material-models/szlab-poly-studio/pyproject.toml")
