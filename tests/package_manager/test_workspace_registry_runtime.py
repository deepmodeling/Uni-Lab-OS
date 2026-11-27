"""工作区（Workspace）目录发布与有限运行激活的 R3 合同测试。"""

from __future__ import annotations

import builtins
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.resource_graph_bootstrap import (
    bootstrap_local_resource_graph,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.package_manager import (
    WorkspaceSource,
    compile_package_source,
    compile_registry_snapshot,
)
from unilabos.package_manager.driver_runtime import (
    DriverActivationError,
    activate_python_driver,
)
from unilabos.package_manager.package_catalog import RegistrySnapshotError
from unilabos.package_manager.workspace_runtime.activation import (
    publish_registry_snapshot,
)
from unilabos.registry.registry import lab_registry
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

WORKFLOW_UUID = "71111111-1111-4111-8111-111111111111"


def _runtime_api() -> ModuleType:
    """读取本轮预先约定的工作区注册表运行时公开接缝。

    参数：无。
    返回：公开 ``WorkspaceRegistryRuntime`` 与准备函数的软件包管理模块。
    异常：接缝尚未实现时以逐项红灯测试（RED Test）失败，不阻断测试收集。
    """

    package_manager = importlib.import_module("unilabos.package_manager")
    # ``required_members`` 是 R3 唯一新增的公开启动接缝，不暴露内部扫描器。
    required_members = (
        "WorkspaceRegistryRuntime",
        "prepare_workspace_registry_runtime",
    )
    missing_members = [
        member for member in required_members if not hasattr(package_manager, member)
    ]
    if missing_members:
        pytest.fail(
            "F03.3-R3 缺少工作区注册表运行时公开接缝: " + ", ".join(missing_members),
            pytrace=False,
        )
    return package_manager


def _write_workspace(
    root: Path,
    *,
    package_id: str = "runtime_lab",
    device_ids: tuple[str, ...] = ("selected_device", "idle_device"),
    resource_ids: tuple[str, ...] = ("selected_rack", "idle_rack"),
    graph_device: str | None = "community.runtime_lab.selected_device",
    graph_resource: str | None = "community.runtime_lab.selected_rack",
    include_workflow: bool = True,
) -> WorkspaceSource:
    """写入包含多定义、显式工作流源码和有限物理图的测试工作区。

    参数：``root`` 是授权工作区根；``package_id`` 是 Python 包身份；
    ``device_ids``/``resource_ids`` 是完整目录定义；``graph_device`` 与
    ``graph_resource`` 是物理图实际选择的身份，传 ``None`` 表示不选择该类定义；
    ``include_workflow`` 决定是否声明固定测试工作流源码（Workflow Source）。
    返回：限定在该根目录的软件包来源 Adapter。
    异常：文件系统写入失败时传播原始异常。
    """

    package_root = root / package_id
    workflow_root = package_root / "workflows"
    workflow_root.mkdir(parents=True)
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    root.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "{package_id.replace("_", "-")}"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    workflow_manifest = "workflows: []\n"
    if include_workflow:
        workflow_manifest = (
            "workflows:\n"
            f"  - workflow_uuid: {WORKFLOW_UUID}\n"
            f"    source: {package_id}/workflows/prepare.py\n"
        )
        workflow_root.joinpath("prepare.py").write_text(
            "from unilabos.workflow.authoring import workflow\n\n"
            f'@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="准备实验")\n'
            "def prepare():\n"
            "    return {}\n",
            encoding="utf-8",
        )
    root.joinpath("package.yaml").write_text(
        f"package:\n  name: {package_id}\n" + workflow_manifest,
        encoding="utf-8",
    )
    for device_id in device_ids:
        # ``module_marker`` 让测试区分静态目录发现与某个具体驱动模块的真实导入。
        module_marker = f"_workspace_runtime_imported_{package_id}_{device_id}"
        symbol = "".join(part.title() for part in device_id.split("_"))
        package_root.joinpath(f"{device_id}.py").write_text(
            "import builtins\n"
            "from unilabos.registry.decorators import device\n\n"
            f"builtins.{module_marker} = True\n\n"
            f'@device(id="{device_id}", category=["test"])\n'
            f"class {symbol}:\n"
            "    pass\n",
            encoding="utf-8",
        )
    for resource_id in resource_ids:
        # ``module_marker`` 证明未选中的资源工厂也没有被启动目录编译导入。
        module_marker = f"_workspace_runtime_imported_{package_id}_{resource_id}"
        package_root.joinpath(f"{resource_id}.py").write_text(
            "import builtins\n"
            "from unilabos.registry.decorators import resource\n\n"
            f"builtins.{module_marker} = True\n\n"
            f'@resource(id="{resource_id}", category=["container"])\n'
            f"def make_{resource_id}(name: str):\n"
            "    return name\n",
            encoding="utf-8",
        )
    graph_nodes: list[dict[str, Any]] = []
    if graph_device is not None:
        graph_nodes.append(
            {
                "id": "selected-device-instance",
                "uuid": "72000000-0000-4000-8000-000000000001",
                "name": "Selected Device",
                "class": graph_device,
                "type": "device",
                "config": {},
                "data": {},
            }
        )
    if graph_resource is not None:
        graph_nodes.append(
            {
                "id": "selected-rack-instance",
                "uuid": "72000000-0000-4000-8000-000000000002",
                "name": "Selected Rack",
                "class": graph_resource,
                "type": "container",
                "config": {},
                "data": {},
            }
        )
    root.joinpath("graph.json").write_text(
        json.dumps({"nodes": graph_nodes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return WorkspaceSource(root)


def _arguments(source: WorkspaceSource) -> dict[str, Any]:
    """构造公共 ``unilab --workspace`` 的最小常驻启动参数。

    参数：``source`` 是当前进程唯一显式授权的工作区来源。
    返回：保留遗留扫描与工作流根字段为空的可变参数字典。
    异常：无。
    """

    return {
        "workspace": str(source.root),
        "graph": "graph.json",
        "devices": None,
        "workflow_editable_package_root": None,
    }


class _NoAstRegistry:
    """只接受完整目录发布、拒绝任何工作区 AST 二次扫描的注册表替身。"""

    def __init__(self) -> None:
        """建立空设备与资源定义集合并初始化扫描计数。

        参数：无。
        返回：无。
        异常：无。
        """

        self.device_type_registry: dict[str, Any] = {}
        self.resource_type_registry: dict[str, Any] = {}
        self.setup_calls = 0

    def setup(self, **_kwargs: Any) -> None:
        """拒绝产品注册表的目录扫描入口。

        参数：``_kwargs`` 是不应出现的扫描配置。
        返回：无。
        异常：始终抛出 ``AssertionError``，证明 R3 发布不再扫描包目录。
        """

        self.setup_calls += 1
        raise AssertionError("工作区目录不得交给 Registry AST 二次扫描")


class _SelectedResourceTree:
    """只暴露物理图选中资源与其一个库位（Site）的资源树。"""

    def dump(self) -> list[list[dict[str, Any]]]:
        """返回一个选中资源物料和一个直属库位的产品资源树快照。

        参数：无。
        返回：与 ``ResourceTreeSet.dump`` 兼容的单树列表。
        异常：无。
        """

        # ``runtime_resource_uuid`` 只负责树内父子关系，库存将生成稳定正式身份。
        runtime_resource_uuid = "72000000-0000-4000-8000-000000000002"
        pose = {
            "position": {"x": 0, "y": 0, "z": 0},
            "size": {"width": 100, "height": 100, "depth": 20},
            "scale": {"x": 1, "y": 1, "z": 1},
            "rotation": {"x": 0, "y": 0, "z": 0},
        }
        return [
            [
                {
                    "id": "selected-rack-instance",
                    "uuid": runtime_resource_uuid,
                    "name": "Selected Rack",
                    "description": "",
                    "parent_uuid": None,
                    "type": "container",
                    "class": "community.runtime_lab.selected_rack",
                    "pose": pose,
                    "config": {},
                    "data": {},
                    "barcode": "",
                },
                {
                    "id": "rack-site-a",
                    "uuid": "72000000-0000-4000-8000-000000000003",
                    "name": "Rack Site A",
                    "description": "",
                    "parent_uuid": runtime_resource_uuid,
                    "type": "well",
                    "class": "",
                    "pose": pose,
                    "config": {},
                    "data": {},
                    "barcode": "",
                },
            ]
        ]


def _prepare_runtime(
    source: WorkspaceSource,
    *,
    compile_catalog: Callable[[WorkspaceSource], Any] = compile_package_source,
):
    """通过公开接缝准备一个尚未发布或导入作者模块的工作区运行计划。

    参数：``source`` 是授权工作区来源；``compile_catalog`` 是可注入的单次完整
    包目录（PackageCatalog）编译接缝。
    返回：公开 ``WorkspaceRegistryRuntime`` 计划与被应用的启动参数。
    异常：工作区、目录、注册表快照或物理图无效时传播公开异常。
    """

    api = _runtime_api()
    startup_arguments = _arguments(source)
    # ``runtime`` 同时持有完整目录、快照、有限选择和工作流源码登记计划。
    runtime = api.prepare_workspace_registry_runtime(
        startup_arguments,
        compile_catalog=compile_catalog,
    )
    assert isinstance(runtime, api.WorkspaceRegistryRuntime)
    return runtime, startup_arguments


def test_workspace_runtime_compiles_catalog_once_without_ast_scan_roots(
    tmp_path: Path,
) -> None:
    """工作区启动只完整编译一次，且不再向注册表传目录扫描根。

    参数：``tmp_path`` 提供隔离工作区。
    返回：无；断言同一包目录（PackageCatalog）只经注入编译接缝一次，遗留
    ``devices`` 保持空。
    异常：重复编译或重新启用 AST 扫描时断言失败。
    """

    source = _write_workspace(tmp_path / "workspace")
    catalog = compile_package_source(source)
    compile_calls: list[WorkspaceSource] = []

    def compile_once(candidate: WorkspaceSource):
        """记录运行时请求的包目录（PackageCatalog）编译。

        参数：``candidate`` 是运行时传入的授权来源。
        返回：预先完整编译的包目录（PackageCatalog）。
        异常：来源身份漂移时断言失败。
        """

        assert candidate.root == source.root
        compile_calls.append(candidate)
        return catalog

    runtime, startup_arguments = _prepare_runtime(
        source,
        compile_catalog=compile_once,
    )

    assert compile_calls == [source]
    assert runtime.catalog is catalog
    assert runtime.registry_snapshot.package_catalogs == (catalog,)
    assert startup_arguments["devices"] is None
    assert startup_arguments["workflow_editable_package_root"] is None


def test_workspace_runtime_reuses_one_immutable_detached_graph_snapshot(
    tmp_path: Path,
) -> None:
    """后续启动消费者必须复用一次读取且可分离的物理图（Graph）快照。

    参数：``tmp_path`` 提供可在准备后改写物理图文件的隔离工作区。
    返回：无；断言运行时保存深度不可变快照，并为每个会修改输入的消费者返回
    互不共享的副本，后续磁盘改写不能改变本次启动代。
    异常：快照仍可被修改、消费者共享容器或再次读取磁盘时测试失败。
    """

    source = _write_workspace(tmp_path / "workspace")
    runtime, _startup_arguments = _prepare_runtime(source)
    # ``fixed_device_fqid`` 是本次启动代已经选中的规范设备定义身份。
    fixed_device_fqid = "community.runtime_lab.selected_device"

    with pytest.raises(TypeError):
        runtime.graph_snapshot["nodes"][0]["class"] = "changed"  # type: ignore[index]

    source.root.joinpath("graph.json").write_text(
        json.dumps({"nodes": []}),
        encoding="utf-8",
    )
    first_consumer_graph = runtime.graph_copy()
    second_consumer_graph = runtime.graph_copy()
    first_consumer_graph["nodes"][0]["class"] = "mutated-by-consumer"

    assert second_consumer_graph["nodes"][0]["class"] == fixed_device_fqid
    assert runtime.graph_copy()["nodes"][0]["class"] == fixed_device_fqid
    assert first_consumer_graph is not second_consumer_graph
    assert first_consumer_graph["nodes"] is not second_consumer_graph["nodes"]


def test_runtime_publishes_complete_catalog_but_selects_finite_graph(
    tmp_path: Path,
) -> None:
    """完整注册表快照可查询，但激活计划只含物理图选择的定义。

    参数：``tmp_path`` 提供含两个设备和两个资源的隔离工作区。
    返回：无；断言发布不调用 AST，完整定义均可查，选择集合保持有限。
    异常：出现二次扫描、遗漏静态定义或激活未选择定义时测试失败。
    """

    source = _write_workspace(tmp_path / "workspace")
    runtime, _startup_arguments = _prepare_runtime(source)
    registry = _NoAstRegistry()

    runtime.publish(registry)

    assert registry.setup_calls == 0
    assert set(registry.device_type_registry) == {
        "community.runtime_lab.idle_device",
        "community.runtime_lab.selected_device",
    }
    assert set(registry.resource_type_registry) == {
        "community.runtime_lab.idle_rack",
        "community.runtime_lab.selected_rack",
    }
    assert runtime.activation_plan.selected_definition_fqids == (
        "community.runtime_lab.selected_device",
        "community.runtime_lab.selected_rack",
    )


def test_author_import_path_activates_only_after_successful_publication(
    tmp_path: Path,
) -> None:
    """作者模块导入路径必须晚于完整注册表快照成功发布。

    参数：``tmp_path`` 提供隔离工作区。
    返回：无；断言发布前激活关闭式失败，发布后才把授权根加入 ``sys.path``。
    异常：发布前改变导入环境或成功发布后不能激活时测试失败。
    """

    source = _write_workspace(tmp_path / "workspace")
    runtime, _startup_arguments = _prepare_runtime(source)
    original_sys_path = list(sys.path)
    try:
        assert str(source.root) not in sys.path
        with pytest.raises(RuntimeError, match="发布|publish"):
            runtime.activate_import_path()
        assert sys.path == original_sys_path

        runtime.publish(_NoAstRegistry())
        runtime.activate_import_path()
        assert sys.path[0] == str(source.root)
    finally:
        sys.path[:] = original_sys_path


def test_workflow_source_plan_reuses_compiled_catalog_without_manifest_reread(
    tmp_path: Path,
) -> None:
    """工作流源码计划来自同一编译代，不应用图也不创建工作流任务。

    参数：``tmp_path`` 提供可在编译后破坏清单的隔离工作区。
    返回：无；断言运行时仍从已编译目录生成来源登记，且启动参数不授权旧扫描根。
    异常：运行时再次读取 ``package.yaml``、应用工作流或要求任务写模型时测试失败。
    """

    source = _write_workspace(tmp_path / "workspace")
    catalog = compile_package_source(source)
    # 编译后故意破坏清单；后续计划必须使用目录内已冻结的来源定义而非重读 YAML。
    source.root.joinpath("package.yaml").write_text("not: [valid", encoding="utf-8")

    def reuse_compiled_catalog(_source: WorkspaceSource) -> object:
        """复用清单破坏前已经冻结的完整包目录。

        参数：``_source`` 是运行时传入的同一工作区来源。
        返回：预编译包目录（PackageCatalog）。
        异常：无。
        """

        return catalog

    runtime, startup_arguments = _prepare_runtime(
        source,
        compile_catalog=reuse_compiled_catalog,
    )

    registrations = runtime.workflow_source_plan.registrations
    assert len(registrations) == 1
    assert registrations[0].workflow_uuid == WORKFLOW_UUID
    assert registrations[0].package_id == "runtime_lab"
    assert registrations[0].package_root == source.root / "runtime_lab"
    assert registrations[0].relative_path == "workflows/prepare.py"
    assert registrations[0].source_uri == ("package://runtime_lab/workflows/prepare.py")
    assert registrations[0].module == "runtime_lab.workflows.prepare"
    assert registrations[0].symbol == "prepare"
    assert registrations[0].definition_content_hash == (
        runtime.catalog.definitions.workflows[0].content_hash
    )
    assert startup_arguments["workflow_editable_package_root"] is None
    assert not hasattr(runtime.workflow_source_plan, "workflow_task")


@pytest.mark.parametrize(
    "graph_identity",
    (
        "community.runtime_lab.selected_device",
        "selected_device",
    ),
    ids=("canonical-fqid", "unique-short-identity"),
)
def test_workspace_selection_activates_only_one_registry_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_identity: str,
) -> None:
    """工作区有限选择以规范全限定身份或唯一短名只激活一个注册表驱动。

    参数：``tmp_path`` 提供隔离工作区；``monkeypatch`` 隔离全局注册表和驱动
    导入；``graph_identity`` 是本例的规范或兼容设备身份。
    返回：无；断言驱动运行时只请求选中模块，未选模块保持零导入。
    异常：工作区选择无法统一解析或目录发布导入未选模块时测试失败。
    """

    source = _write_workspace(
        tmp_path / "workspace",
        graph_device=graph_identity,
        graph_resource=None,
    )
    runtime, _startup_arguments = _prepare_runtime(source)
    monkeypatch.setattr(lab_registry, "device_type_registry", {})
    monkeypatch.setattr(lab_registry, "resource_type_registry", {})
    monkeypatch.setattr(lab_registry, "_package_snapshot", None, raising=False)
    monkeypatch.delattr(
        builtins,
        "_workspace_runtime_imported_runtime_lab_idle_device",
        raising=False,
    )
    runtime.publish(lab_registry)

    imported_modules: list[str] = []

    class SelectedDriver:
        """记录设备实例化参数的无硬件测试驱动。"""

        def __init__(self, **_kwargs: Any) -> None:
            """接受设备包装器传入的运行参数而不建立物理连接。

            参数：``_kwargs`` 是设备身份与初始化配置。
            返回：无。
            异常：无。
            """

    def get_selected_class(module: str) -> type[SelectedDriver]:
        """记录注册表最终解析出的驱动模块身份。

        参数：``module`` 是 ``module:symbol`` 形式的作者实现身份。
        返回：不访问硬件的选中设备驱动类。
        异常：无。
        """

        imported_modules.append(module)
        return SelectedDriver

    # ``activation`` 是工作区物理图有限选择产生的唯一驱动激活结果。
    activation = activate_python_driver(
        lab_registry,
        graph_identity,
        {},
        loader=get_selected_class,
    )

    assert activation.driver_class is SelectedDriver
    assert activation.definition_identity == "community.runtime_lab.selected_device"
    assert imported_modules == ["runtime_lab.selected_device:SelectedDevice"]
    assert not hasattr(
        builtins,
        "_workspace_runtime_imported_runtime_lab_idle_device",
    )


def test_workspace_ambiguous_short_identity_fails_before_device_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨包重复短名必须在设备导入前关闭式失败。

    参数：``tmp_path`` 提供两个隔离包；``monkeypatch`` 隔离产品注册表代际。
    返回：无；断言统一解析器拒绝歧义短名，且两个作者模块均未导入。
    异常：若解析器任意选择一个包，断言失败。
    """

    first = _write_workspace(
        tmp_path / "first",
        package_id="first_lab",
        device_ids=("shared_device",),
        resource_ids=(),
        graph_device=None,
        graph_resource=None,
        include_workflow=False,
    )
    second = _write_workspace(
        tmp_path / "second",
        package_id="second_lab",
        device_ids=("shared_device",),
        resource_ids=(),
        graph_device=None,
        graph_resource=None,
        include_workflow=False,
    )
    snapshot = compile_registry_snapshot(
        (compile_package_source(first), compile_package_source(second))
    )
    monkeypatch.setattr(lab_registry, "device_type_registry", {})
    monkeypatch.setattr(lab_registry, "resource_type_registry", {})
    monkeypatch.setattr(lab_registry, "_package_snapshot", None, raising=False)
    publish_registry_snapshot(snapshot, lab_registry)

    imported_modules: list[str] = []

    def reject_load(source_identity: str) -> type[object]:
        """记录歧义解析后不应发生的作者驱动加载。

        参数：``source_identity`` 是意外请求的驱动源码身份。
        返回：普通测试类，仅为满足加载器接口。
        异常：无。
        """

        imported_modules.append(source_identity)
        return object

    with pytest.raises(DriverActivationError) as caught:
        activate_python_driver(
            lab_registry,
            "shared_device",
            {},
            loader=reject_load,
        )

    assert caught.value.code == "definition_resolution_error"
    assert isinstance(caught.value.__cause__, RegistrySnapshotError)
    assert imported_modules == []
    assert "first_lab.shared_device" not in sys.modules
    assert "second_lab.shared_device" not in sys.modules


def test_selected_resource_bootstraps_material_and_site_without_idle_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有限资源图复用现有库存启动投影，只创建选中物料与库位。

    参数：``tmp_path`` 提供隔离工作区；``monkeypatch`` 隔离产品注册表代际。
    返回：无；断言完整资源模板可查询，但库存权威只产生图中资源及直属库位。
    异常：未选资源被投影、模板解析失败或库存事务部分提交时测试失败。
    """

    source = _write_workspace(
        tmp_path / "workspace",
        graph_device=None,
    )
    runtime, _startup_arguments = _prepare_runtime(source)
    monkeypatch.setattr(lab_registry, "device_type_registry", {})
    monkeypatch.setattr(lab_registry, "resource_type_registry", {})
    monkeypatch.setattr(lab_registry, "_package_snapshot", None, raising=False)
    runtime.publish(lab_registry)
    # ``template_snapshot`` 是完整静态模板代；具体库存仍只由有限资源树决定。
    template_snapshot = RegistryTemplateSnapshot.from_registry(lab_registry)
    store = InventoryStore(":memory:")
    try:
        receipt = bootstrap_local_resource_graph(
            store=store,
            resource_tree_set=_SelectedResourceTree(),
            registry_snapshot=template_snapshot,
            source_id=str(source.root / "graph.json"),
        )
        material_graph = BackendResourceService(store).material_graph()
    finally:
        store.close()

    assert receipt["material_count"] == 1
    assert receipt["site_count"] == 1
    assert [node["material"]["name"] for node in material_graph["nodes"]] == [
        "Selected Rack"
    ]
    assert [site["name"] for site in material_graph["nodes"][0]["sites"]] == [
        "Rack Site A"
    ]
    assert all(
        node["material"]["name"] != "Idle Rack" for node in material_graph["nodes"]
    )
