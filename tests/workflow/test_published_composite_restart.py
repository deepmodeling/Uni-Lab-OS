"""已发布子工作流在重启后仍可展开父工作流的回归测试。"""

from __future__ import annotations

import uuid
from pathlib import Path

from tests.registry.test_f05_material_source_catalog import (
    _ActionRegistry,
    _Registry,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.source_discovery import discover_editable_sources


class _RegistryWithPump(_Registry):
    """提供测试所需的一个可执行动作模板。"""

    def obtain_registry_device_info(self) -> list[dict[str, object]]:
        """返回宿主节点与泵动作模板。

        参数：无。返回：包含基础宿主节点和一个 ``prime`` 动作的注册表定义，
        用于让子工作流拥有真实的展开节点。异常：注册表构造失败时原样传播。
        """

        return super().obtain_registry_device_info() + [
            _ActionRegistry().obtain_registry_device_info()[0]
        ]


def _write_package_root(root: Path) -> None:
    """创建一个可被导入接口写入工作流源码的领域包目录。

    参数：``root`` 是测试专用包根目录。返回：无；写入包元数据和 Python 包
    初始化文件。异常：目录或文件写入失败时原样传播。
    """

    root.mkdir()
    root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "restart-composite-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    root.joinpath("package.yaml").write_text(
        "package:\n  name: restart_composite_lab\nworkflows: []\n",
        encoding="utf-8",
    )
    package_dir = root / "restart_composite_lab"
    package_dir.mkdir()
    package_dir.joinpath("__init__.py").write_text("", encoding="utf-8")


def _child_source(workflow_uuid: str) -> str:
    """返回一个包含真实设备动作的实验操作源码。

    参数：``workflow_uuid`` 是子工作流的稳定身份。返回：带 ``prime`` 设备动作
    的 Python 源码字符串。异常：无；源码内容由固定测试模板组成。
    """

    return f'''from lab.devices import Pump
from unilabos.workflow.authoring import device, workflow, workflow_output

pump: Pump = device()

@workflow(
    workflow_uuid="{workflow_uuid}",
    displayname="Restart child",
    workflow_type="experiment_operation",
)
def child():
    # unilab:node_uuid=6b4a4f4b-9029-4ee2-8dc8-43c42a53f9ef
    result = pump.prime()
    return workflow_output()
'''


def _parent_source(workflow_uuid: str) -> str:
    """返回调用已发布子工作流的父工作流源码。

    参数：``workflow_uuid`` 是父工作流的稳定身份。返回：导入并调用子工作流的
    Python 源码字符串。异常：无；源码内容由固定测试模板组成。
    """

    return f'''from restart_composite_lab.experiment_operations.child import child
from unilabos.workflow.authoring import workflow, workflow_output

@workflow(workflow_uuid="{workflow_uuid}", displayname="Restart parent")
def parent():
    # unilab:node_uuid=4fcacda7-dca4-4db0-a972-8d50d8fce6af
    result = child()
    return workflow_output()
'''


def test_parent_composite_graph_survives_real_restart(tmp_path: Path) -> None:
    """父工作流重启后应按领域包合同重新展开已发布子工作流。

    参数：``tmp_path`` 隔离领域包、工作流运行事实与库存数据库。返回：无；
    首次导入并发布子工作流后，父工作流展开为组合节点加子动作节点，重启后
    仍保持相同结构且没有编译诊断。异常：若启动先编译父源码、后恢复子合同，
    父图会出现 ``composite_child_not_found``，测试将失败。
    """

    reset_workflow_service_for_test()
    package_root = tmp_path / "editable"
    _write_package_root(package_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    child_uuid = str(uuid.uuid4())
    parent_uuid = str(uuid.uuid4())
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RegistryWithPump(),
            editable_package_roots=(package_root,),
            start_source_monitor=False,
        )
        service.import_python_workflow(
            file_name="child.py",
            python_source=_child_source(child_uuid),
        )
        service.publish_workflow_contract(child_uuid, revision=1)
        parent = service.import_python_workflow(
            file_name="parent.py",
            python_source=_parent_source(parent_uuid),
        )
        assert len(parent["nodes"]) >= 2
        assert {node["type"] for node in parent["nodes"]} >= {"workflow", "ILab"}

        reset_workflow_service_for_test()
        plan = discover_editable_sources((package_root,))
        restarted, _restarted_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RegistryWithPump(),
            editable_source_discovery_plan=plan,
            start_source_monitor=False,
        )
        authoring = restarted.get_authoring(parent_uuid)
        graph = restarted.get_graph(parent_uuid)
        assert authoring["state"] == "applied"
        assert authoring["draft"]["diagnostics"] == []
        assert len(graph["nodes"]) >= 2
        assert {node["type"] for node in graph["nodes"]} >= {"workflow", "ILab"}
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_parent_composite_revision_two_survives_real_restart(tmp_path: Path) -> None:
    """合同修订大于 1 时冷启动也不能把源码应用再推进一个版本。

    参数：``tmp_path`` 隔离领域包、工作流运行事实与库存数据库。返回：无；先
    把子工作流源码改成第二个图修订并发布，再重启验证子修订仍为 2，父图仍能
    展开。异常：若启动骨架总从 revision 1 递增，子会变成 3、父合同解析失败。
    """

    reset_workflow_service_for_test()
    package_root = tmp_path / "editable"
    _write_package_root(package_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    child_uuid = str(uuid.uuid4())
    parent_uuid = str(uuid.uuid4())
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RegistryWithPump(),
            editable_package_roots=(package_root,),
            start_source_monitor=False,
        )
        service.import_python_workflow(
            file_name="child.py",
            python_source=_child_source(child_uuid),
        )
        child_path = (
            package_root
            / "restart_composite_lab"
            / "experiment_operations"
            / "child.py"
        )
        child_path.write_text(
            _child_source(child_uuid).replace("Restart child", "Restart child v2"),
            encoding="utf-8",
        )
        observed_signature = service.source_signature(child_uuid)
        assert service.submit_source_change(
            child_uuid,
            observed_signature=observed_signature,
        )
        candidate = service.get_authoring(child_uuid)["candidate"]
        assert isinstance(candidate, dict)
        applied = service.apply_authoring(
            child_uuid,
            candidate_hash=str(candidate["candidate_hash"]),
        )
        assert applied["apply_result"]["workflow_revision"] == 2
        service.publish_workflow_contract(child_uuid, revision=2)
        parent = service.import_python_workflow(
            file_name="parent.py",
            python_source=_parent_source(parent_uuid),
        )
        assert len(parent["nodes"]) >= 2

        reset_workflow_service_for_test()
        plan = discover_editable_sources((package_root,))
        restarted, _restarted_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RegistryWithPump(),
            editable_source_discovery_plan=plan,
            start_source_monitor=False,
        )
        assert restarted.get_workflow(child_uuid)["revision"] == 2
        authoring = restarted.get_authoring(parent_uuid)
        graph = restarted.get_graph(parent_uuid)
        assert authoring["state"] == "applied"
        assert authoring["draft"]["diagnostics"] == []
        assert len(graph["nodes"]) >= 2
        assert {node["type"] for node in graph["nodes"]} >= {"workflow", "ILab"}
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_three_level_composite_contains_each_leaf_once(tmp_path: Path) -> None:
    reset_workflow_service_for_test()
    package_root = tmp_path / "editable"
    _write_package_root(package_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    identities = [str(uuid.uuid4()) for _ in range(3)]
    try:
        service, _ = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RegistryWithPump(),
            editable_package_roots=(package_root,),
            start_source_monitor=False,
        )
        service.import_python_workflow(
            file_name="child.py", python_source=_child_source(identities[0])
        )
        service.publish_workflow_contract(identities[0], revision=1)
        parent_source = _parent_source(identities[1]).replace(
            'displayname="Restart parent"',
            'displayname="Restart parent", workflow_type="experiment_operation"',
        )
        service.import_python_workflow(
            file_name="parent.py", python_source=parent_source
        )
        service.publish_workflow_contract(identities[1], revision=1)
        grand_source = (
            _parent_source(identities[2])
            .replace(
                "experiment_operations.child import child",
                "experiment_operations.parent import parent",
            )
            .replace("def parent():", "def grand():")
            .replace("result = child()", "result = parent()")
            .replace("4fcacda7-dca4-4db0-a972-8d50d8fce6af", str(uuid.uuid4()))
        )
        graph = service.import_python_workflow(
            file_name="grand.py", python_source=grand_source
        )
        assert len(graph["nodes"]) == 3
        assert sum(node["type"] == "ILab" for node in graph["nodes"]) == 1
        reset_workflow_service_for_test()
        restarted, _ = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RegistryWithPump(),
            editable_source_discovery_plan=discover_editable_sources((package_root,)),
            start_source_monitor=False,
        )
        restored = restarted.get_graph(identities[2])
        assert len(restored["nodes"]) == 3
        assert sum(node["type"] == "ILab" for node in restored["nodes"]) == 1
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()
