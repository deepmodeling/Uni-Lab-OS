"""纯静态包目录（PackageCatalog）编译合同。"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"


def _write_package(workspace_root: Path, *, broken_source: bool = False) -> None:
    """写入包含设备、资源和显式工作流源码的测试包。

    参数：``workspace_root`` 是显式软件包来源根；``broken_source`` 决定是否加入
    一个语法损坏文件以验证关闭式失败。
    返回：无；写入完整静态编译输入。
    异常：文件系统写入失败时向测试传播原始异常。
    """

    # ``package_root`` 是包目录（PackageCatalog）允许读取的 Python 包边界。
    package_root = workspace_root / "catalog_lab"
    workflow_root = package_root / "workflows"
    workflow_root.mkdir(parents=True)
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    workspace_root.joinpath("pyproject.toml").write_text(
        "[project]\n"
        'name = "catalog-lab"\n'
        'version = "1.2.3"\n'
        'description = "静态目录测试包"\n'
        'dependencies = ["typing-extensions>=4"]\n',
        encoding="utf-8",
    )
    workspace_root.joinpath("package.yaml").write_text(
        "package:\n"
        "  name: catalog_lab\n"
        "workflows:\n"
        f"  - workflow_uuid: {WORKFLOW_UUID}\n"
        "    source: catalog_lab/workflows/prepare.py\n",
        encoding="utf-8",
    )
    package_root.joinpath("definitions.py").write_text(
        "import builtins\n"
        "from typing import TypedDict\n"
        "from unilabos.registry.decorators import action, device, resource\n"
        "from unilabos.registry.placeholder_type import ResourceSlot\n\n"
        "builtins._package_catalog_source_imported = True\n\n"
        "class Result(TypedDict):\n"
        "    plate: ResourceSlot\n\n"
        '@device(id="reactor", displayname="反应器", category=["reactor"])\n'
        "class Reactor:\n"
        '    @action(description="处理物料")\n'
        "    def process(self, plate: ResourceSlot) -> Result:\n"
        '        return {"plate": plate}\n\n'
        '@resource(id="plate", displayname="孔板", category=["container"])\n'
        "def make_plate(name: str):\n"
        "    return name\n",
        encoding="utf-8",
    )
    workflow_root.joinpath("prepare.py").write_text(
        "from unilabos.workflow.authoring import workflow\n\n"
        f'@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="准备实验")\n'
        "def prepare():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    package_root.joinpath("models", "plate.glb").parent.mkdir()
    package_root.joinpath("models", "plate.glb").write_bytes(b"glTF-test")
    if broken_source:
        package_root.joinpath("broken.py").write_text(
            "def broken(:\n",
            encoding="utf-8",
        )


def test_compile_package_source_discovers_complete_catalog_without_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明完整静态编译发现定义和资产，但不执行作者源码。

    参数：``tmp_path`` 提供隔离软件包来源；``monkeypatch`` 清除进程级导入哨兵。
    返回：无；断言设备、资源、工作流源码（Workflow Source）、动作物料锁
    （Action Material Lock）和资产进入同一不可变目录。
    异常：缺失公开编译缝或发生作者源码导入时测试失败。
    """

    from unilabos.package_manager import WorkspaceSource, compile_package_source

    # ``workspace_root`` 是本轮唯一显式软件包来源身份。
    workspace_root = tmp_path / "workspace"
    _write_package(workspace_root)
    monkeypatch.delattr(
        builtins,
        "_package_catalog_source_imported",
        raising=False,
    )

    # ``catalog`` 是完整校验后一次发布的包目录（PackageCatalog）候选。
    catalog = compile_package_source(WorkspaceSource(workspace_root))

    assert catalog.distribution.name == "catalog-lab"
    assert catalog.distribution.version == "1.2.3"
    assert catalog.import_package == "catalog_lab"
    assert catalog.namespace == "community.catalog_lab"
    assert [item.id for item in catalog.definitions.devices] == ["reactor"]
    assert [item.fqid for item in catalog.definitions.resources] == [
        "community.catalog_lab.plate"
    ]
    assert catalog.definitions.resources[0].details["registry_entry"]["source_uri"] == (
        "package://catalog_lab/definitions.py"
    )
    assert [item.id for item in catalog.definitions.workflows] == ["prepare"]
    assert catalog.definitions.workflows[0].details["workflow_uuid"] == WORKFLOW_UUID
    assert catalog.definitions.workflows[0].details["source_uri"] == (
        "package://catalog_lab/workflows/prepare.py"
    )
    # ``process_schema`` 是注册表（Registry）规范动作合同的 JSON Schema 投影。
    process_schema = catalog.definitions.devices[0].details["registry_entry"]["class"][
        "action_value_mappings"
    ]["process"]["schema"]
    assert (
        process_schema["properties"]["goal"]["properties"]["plate"][
            "x-unilabos-material-lock"
        ]
        is True
    )
    assert [item.logical_path for item in catalog.assets] == [
        "catalog_lab/models/plate.glb"
    ]
    assert not hasattr(builtins, "_package_catalog_source_imported")


def test_catalog_digest_is_independent_of_absolute_workspace_path(
    tmp_path: Path,
) -> None:
    """证明相同软件包内容在不同绝对目录产生相同规范摘要。

    参数：``tmp_path`` 提供两个内容一致但路径不同的软件包来源。
    返回：无；断言目录摘要和规范字节完全一致。
    异常：若绝对路径或修改时间泄漏进目录，断言失败。
    """

    from unilabos.package_manager import WorkspaceSource, compile_package_source

    # ``first_root`` 与 ``second_root`` 模拟两个开发者机器上的同一包内容。
    first_root = tmp_path / "first" / "workspace"
    second_root = tmp_path / "second" / "workspace"
    _write_package(first_root)
    _write_package(second_root)

    first_catalog = compile_package_source(WorkspaceSource(first_root))
    second_catalog = compile_package_source(WorkspaceSource(second_root))

    assert first_catalog.content_digest == second_catalog.content_digest
    assert first_catalog.catalog_digest == second_catalog.catalog_digest
    assert first_catalog.to_canonical_bytes() == second_catalog.to_canonical_bytes()


def test_catalog_excludes_agent_native_skill_projections(
    tmp_path: Path,
) -> None:
    """AionUi 的原生技能链接不得进入目录摘要或破坏安全静态编译。"""

    from unilabos.package_manager import WorkspaceSource, compile_package_source

    workspace_root = tmp_path / "workspace"
    _write_package(workspace_root)
    package_root = workspace_root / "catalog_lab"
    private_skill = workspace_root / ".unilabos" / "agent" / "skill"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("runtime skill\n", encoding="utf-8")
    for native_root in (".claude", ".codex"):
        skill_link = package_root / native_root / "skills" / "runtime-skill"
        skill_link.parent.mkdir(parents=True)
        skill_link.symlink_to(private_skill)

    projected = compile_package_source(WorkspaceSource(workspace_root))
    for native_root in (".claude", ".codex"):
        (package_root / native_root / "skills" / "runtime-skill").unlink()
    clean = compile_package_source(WorkspaceSource(workspace_root))

    assert projected.content_digest == clean.content_digest
    assert projected.catalog_digest == clean.catalog_digest


def test_any_python_syntax_error_rejects_the_complete_catalog(
    tmp_path: Path,
) -> None:
    """证明包内任意 Python 语法错误都会关闭式拒绝完整目录。

    参数：``tmp_path`` 提供含损坏文件的隔离软件包来源。
    返回：无；断言错误含稳定诊断码且不返回部分设备或工作流定义。
    异常：公开编译器应抛出 ``PackageCompileError``。
    """

    from unilabos.package_manager import (
        PackageCompileError,
        WorkspaceSource,
        compile_package_source,
    )

    # ``workspace_root`` 是必须整体失败、不能部分发布的软件包来源。
    workspace_root = tmp_path / "workspace"
    _write_package(workspace_root, broken_source=True)

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(workspace_root))

    assert [item.code for item in caught.value.diagnostics] == ["python_syntax_error"]
    assert caught.value.diagnostics[0].path == "catalog_lab/broken.py"


def test_empty_package_compiles_to_a_stable_empty_definition_catalog(
    tmp_path: Path,
) -> None:
    """证明新包骨架可形成零定义但有稳定身份的包目录（PackageCatalog）。

    参数：``tmp_path`` 提供隔离空包来源。
    返回：无；断言三类定义与资产为空，摘要仍存在。
    异常：空包若被误判为损坏，公开编译器会抛出异常并使测试失败。
    """

    from unilabos.package_manager import WorkspaceSource, compile_package_source

    # ``workspace_root`` 是尚未声明设备、资源或工作流的合法开发包。
    workspace_root = tmp_path / "workspace"
    package_root = workspace_root / "empty_lab"
    package_root.mkdir(parents=True)
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    workspace_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "empty-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    workspace_root.joinpath("package.yaml").write_text(
        "package: {name: empty_lab}\nworkflows: []\n",
        encoding="utf-8",
    )

    catalog = compile_package_source(WorkspaceSource(workspace_root))

    assert catalog.definitions.devices == ()
    assert catalog.definitions.resources == ()
    assert catalog.definitions.workflows == ()
    assert catalog.assets == ()
    assert catalog.catalog_digest.startswith("sha256:")


def test_legacy_no_output_workflow_compiles_as_empty_output_contract(
    tmp_path: Path,
) -> None:
    """证明遗留无输出工作流可进入完整包目录（PackageCatalog）。

    参数：``tmp_path`` 提供隔离工作区并复用完整设备/资源夹具。
    返回：无；断言旧 ``@workflow_definition``、``-> None`` 与隐式函数返回被
    规范化为空工作流输出合同（Workflow Output Contract）。
    异常：动作或输入的其他静态错误仍必须由完整编译关闭式拒绝。
    """

    from unilabos.package_manager import WorkspaceSource, compile_package_source

    # ``workspace_root`` 模拟当前 SZLab 中尚未改写为显式 ``workflow_output`` 的源码。
    workspace_root = tmp_path / "workspace"
    _write_package(workspace_root)
    workspace_root.joinpath("catalog_lab/workflows/child.py").write_text(
        "def child(*, value):\n    return value\n",
        encoding="utf-8",
    )
    workspace_root.joinpath("catalog_lab/workflows/prepare.py").write_text(
        "from catalog_lab.workflows.child import child\n"
        "from unilabos.workflow.authoring import resource_ref, workflow_definition\n\n"
        f'@workflow_definition(workflow_uuid="{WORKFLOW_UUID}", displayname="准备实验")\n'
        "def prepare(*, batch: int = 1) -> None:\n"
        "    # unilab:node_uuid=22222222-2222-4222-8222-222222222222\n"
        '    run = child(value=resource_ref("warehouse"))\n',
        encoding="utf-8",
    )

    # ``catalog`` 只兼容无输出表达，不绕过动作、身份或文件安全检查。
    catalog = compile_package_source(WorkspaceSource(workspace_root))

    workflow = catalog.definitions.workflows[0]
    assert workflow.details["input_contract"]["parameters"][0]["name"] == "batch"
    assert workflow.details["output_contract"] == ()
    assert workflow.details["action_references"] == (
        {
            "kind": "workflow",
            "module": "catalog_lab.workflows.child",
            "symbol": "child",
        },
    )
