"""F06 R4 已发布工作流目录的生产组合与重启恢复 RED。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.registry.test_f05_material_source_catalog import _Registry
from tests.registry.test_template_projection import FakeRegistry
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.workflow_template_api import WorkflowTemplateQueryService
from unilabos.registry.template_identity import device_template_uuid
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import WorkflowStore

from .test_c1_r2_static_expansion_contract import (
    CHILD_WORKFLOW_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
)

PACKAGE_ID = "c1_product_lab"
CHILD_MODULE = f"{PACKAGE_ID}.workflows.child"


def _child_source() -> str:
    """返回可被启动恢复与静态目录共同消费的空叶工作流源码。

    参数：无。返回：固定身份的空叶工作流 Python 源码。异常：无。
    """

    return f'''from unilabos.workflow.authoring import workflow, workflow_output


@workflow(
    workflow_uuid="{CHILD_WORKFLOW_UUID}",
    displayname="Published child",
    workflow_type="experiment_operation",
)
def prepare_sample():
    return workflow_output()
'''


def _write_package(selected_root: Path) -> None:
    """写入子/父工作流显式授权的可编辑包（Editable Package）。

    参数：``selected_root`` 是测试独占包根。返回：无；在包根写入两项源码和
    一份声明。异常：目录创建或写文件失败时原样抛出 ``OSError``。
    """

    source_path = selected_root / PACKAGE_ID / "workflows" / "child.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_child_source(), encoding="utf-8")
    parent_path = selected_root / PACKAGE_ID / "workflows" / "parent.py"
    parent_path.write_text(_parent_source(), encoding="utf-8")
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        f"  name: {PACKAGE_ID}\n"
        "workflows:\n"
        f"  - workflow_uuid: {CHILD_WORKFLOW_UUID}\n"
        f"    source: {PACKAGE_ID}/workflows/child.py\n"
        f"  - workflow_uuid: {PARENT_WORKFLOW_UUID}\n"
        f"    source: {PACKAGE_ID}/workflows/parent.py\n",
        encoding="utf-8",
    )


def _parent_source() -> str:
    """返回只调用发布叶工作流、不创建任务或物理动作的父作者源码。

    参数：无。返回：带稳定调用节点身份的父工作流 Python 源码。异常：无。
    """

    return f'''from {CHILD_MODULE} import prepare_sample
from unilabos.workflow.authoring import workflow, workflow_output


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Product parent",
)
def product_parent():
    # unilab:node_uuid={INVOCATION_UUID}
    result = prepare_sample()
    return workflow_output()
'''


def _empty_parent_graph() -> dict[str, object]:
    """构造生产编译器首次调用使用的空父工作流图。

    参数：无。返回：修订为一的空父图。异常：无。
    """

    return {
        "workflow": {
            "uuid": PARENT_WORKFLOW_UUID,
            "revision": 1,
            "name": "Product parent",
            "tags": [],
            "description": None,
            "meta_data": {},
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _contract_extension(template: object) -> dict[str, object]:
    """从目录模板对象或持久 JSON 文本读取发布工作流扩展。

    参数：``template`` 是目录模板字典，其中 Schema 可为字典或 JSON 文本。
    返回：发布工作流合同扩展。异常：形状错误时由断言或 JSON 解析抛出。
    """

    assert isinstance(template, dict)
    schema = template["schema"]
    if isinstance(schema, str):
        schema = json.loads(schema)
    assert isinstance(schema, dict)
    extension = schema["x-unilabos-workflow-contract"]
    assert isinstance(extension, dict)
    return extension


def test_product_composition_publishes_and_rebuilds_workflow_templates(
    tmp_path: Path,
) -> None:
    """生产组合发布同代工作流模板，并在重启后从源码重建相同身份。

    参数：``tmp_path`` 隔离产品数据库与包根。返回：无；断言发布、应用重建与
    重启重建。异常：产品组合或断言失败时由 pytest 报告。
    """

    reset_workflow_service_for_test()
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_package(selected_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_package_roots=(selected_root,),
        )
        action = projection.snapshot().require_action(
            f"{CHILD_MODULE}:prepare_sample",
            f"workflow:{CHILD_WORKFLOW_UUID}",
        )
        first_template_uuid = str(action.template["uuid"])
        assert action.template["type"] == "workflow"
        assert service.compiler is not None
        compiled = service.compiler.compile(
            workflow_uuid=PARENT_WORKFLOW_UUID,
            workflow_revision=1,
            python_source=_parent_source(),
            source_uri=f"package://{PACKAGE_ID}/workflows/parent.py",
            applied_graph=_empty_parent_graph(),
        )
        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        query = WorkflowTemplateQueryService(projection)
        page = query.list_node_templates(
            page=1,
            page_size=20,
            keyword="",
            resource_template_uuid=None,
            action_type="",
            node_type="workflow",
        )
        assert {item["name"] for item in page["items"]} == {
            f"workflow:{CHILD_WORKFLOW_UUID}", f"workflow:{PARENT_WORKFLOW_UUID}",
        }
        assert first_template_uuid in {item["uuid"] for item in page["items"]}

        authoring = service.get_authoring(CHILD_WORKFLOW_UUID)
        assert authoring["state"] == "applied"
        assert authoring["candidate"] is None
        assert authoring["draft"]["diagnostics"] == []
        refreshed = projection.snapshot().require_action(
            f"{CHILD_MODULE}:prepare_sample",
            f"workflow:{CHILD_WORKFLOW_UUID}",
        )
        extension = _contract_extension(refreshed.detached_template())
        assert extension["workflow_revision"] == service.get_graph(
            CHILD_WORKFLOW_UUID
        )["workflow"]["revision"]
        assert service.compiler is not None
        assert service.compiler.template_catalog_fingerprint == (
            projection.snapshot().fingerprint
        )
        from contextlib import closing

        from unilabos.persistence import open_database
        with closing(open_database(str(tmp_path / "workflow_history.db"), "workflow")) as connection:
            assert {
                "workflow_node_template",
                "workflow_handle_template",
                "workflow",
                "workflow_node",
            }.isdisjoint(connection.table_names())

        reset_workflow_service_for_test()
        restarted, restarted_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_package_roots=(selected_root,),
        )
        restored = restarted_projection.snapshot().require_action(
            f"{CHILD_MODULE}:prepare_sample",
            f"workflow:{CHILD_WORKFLOW_UUID}",
        )
        assert restored.template["uuid"] == first_template_uuid
        assert restarted.compiler is not None
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_workspace_capability_loads_unpublished_composite_child(
    tmp_path: Path,
) -> None:
    """工作区能力允许未发布子工作流时仍应完成父图组合和启动恢复。

    参数：``tmp_path`` 隔离包目录与库存数据库。返回：即使包根已经存在严格
    发布目录，显式工作区来源能力仍把活动源码加入本地组合目录；父工作流能够
    展开子图且不产生 ``composite_child_not_found``。异常：若未发布来源继续
    阻断固定点激活，断言会直接报告启动失败。
    """

    reset_workflow_service_for_test()
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_package(selected_root)
    # 合法空发布目录模拟真实包已经进入严格合同模式，但本测试刻意不发布
    # child，验证本地组合例外不会修改该文件。
    publication_path = selected_root / PACKAGE_ID / "workflow_publications.json"
    publication_path.write_text(
        '{"version": 1, "publications": []}\n',
        encoding="utf-8",
    )
    publication_before = publication_path.read_bytes()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_package_roots=(selected_root,),
            start_source_monitor=False,
            allow_unpublished_composite_sources=True,
        )
        child_authoring = service.get_authoring(CHILD_WORKFLOW_UUID)
        parent_authoring = service.get_authoring(PARENT_WORKFLOW_UUID)
        assert child_authoring["state"] == "applied"
        assert parent_authoring["state"] == "applied"
        assert parent_authoring["draft"]["diagnostics"] == []
        parent_graph = service.get_graph(PARENT_WORKFLOW_UUID)
        assert {node["type"] for node in parent_graph["nodes"]} >= {"workflow"}
        assert projection.snapshot().require_action(
            f"{CHILD_MODULE}:prepare_sample",
            f"workflow:{CHILD_WORKFLOW_UUID}",
        )
        assert publication_path.read_bytes() == publication_before
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


@pytest.mark.parametrize("allow_unpublished", [True, False])
def test_three_level_workspace_composition_rebuilds_fresh_template_closure(
    tmp_path: Path, allow_unpublished: bool,
) -> None:
    """三级源码组合在重启后保持可用，严格模式仍拒绝未发布来源。"""

    reset_workflow_service_for_test()
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_package(selected_root)
    grandparent_uuid = "d38c6491-c068-4edb-a12d-017e439b26f5"
    parent_path = selected_root / PACKAGE_ID / "workflows" / "parent.py"
    parent_path.write_text(_parent_source().replace(
        'displayname="Product parent",',
        'displayname="Product parent", workflow_type="experiment_operation",',
    ), encoding="utf-8")
    grandparent = selected_root / PACKAGE_ID / "workflows" / "grandparent.py"
    grandparent.write_text(
        f'''from {PACKAGE_ID}.workflows.parent import product_parent
from unilabos.workflow.authoring import workflow, workflow_output

@workflow(workflow_uuid="{grandparent_uuid}", displayname="Grandparent")
def grandparent():
    # unilab:node_uuid=1941a0a9-b08d-4eea-825b-f93a04de2f76
    result = product_parent()
    return workflow_output()
''', encoding="utf-8",
    )
    manifest = selected_root / "package.yaml"
    manifest.write_text(
        manifest.read_text() + f"  - workflow_uuid: {grandparent_uuid}\n"
        f"    source: {PACKAGE_ID}/workflows/grandparent.py\n", encoding="utf-8",
    )
    publication_path = selected_root / PACKAGE_ID / "workflow_publications.json"
    publication_path.write_text('{"version": 1, "publications": []}\n', encoding="utf-8")
    publication_before = publication_path.read_bytes()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        for _restart in range(2):
            service, projection = compose_local_workflow_template_runtime(
                tmp_path, inventory_store=inventory_store, registry=_Registry(),
                editable_package_roots=(selected_root,), start_source_monitor=False,
                allow_unpublished_composite_sources=allow_unpublished,
            )
            authoring = service.get_authoring(grandparent_uuid)
            if allow_unpublished:
                assert authoring["state"] == "applied", authoring["draft"]["diagnostics"]
                assert projection.snapshot().require_action(
                    f"{PACKAGE_ID}.workflows.parent:product_parent",
                    f"workflow:{PARENT_WORKFLOW_UUID}",
                )
            else:
                assert authoring["state"] != "applied"
            assert publication_path.read_bytes() == publication_before
            if allow_unpublished and _restart == 1:
                # 同进程撤销叶来源，不能借用上一代叶模板继续投影父、祖父。
                manifest.write_text(manifest.read_text().replace(
                    f"  - workflow_uuid: {CHILD_WORKFLOW_UUID}\n"
                    f"    source: {PACKAGE_ID}/workflows/child.py\n", "",
                ), encoding="utf-8")
                service.replace_discovered_source_authorizations(
                    discover_editable_sources((selected_root,)),
                )
                service._rebuild_workspace_activation_catalog()
                assert not [action for action in projection.snapshot().actions
                            if action.template.get("type") == "workflow"]
            reset_workflow_service_for_test()
        if allow_unpublished:
            service, projection = compose_local_workflow_template_runtime(
                tmp_path, inventory_store=inventory_store, registry=_Registry(),
                editable_package_roots=(selected_root,), start_source_monitor=False,
                allow_unpublished_composite_sources=True,
            )
            assert service.get_authoring(grandparent_uuid)["state"] != "applied"
            assert not [action for action in projection.snapshot().actions
                        if action.template.get("type") == "workflow"]
            assert publication_path.read_bytes() == publication_before
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_workspace_skips_malformed_unpublished_child_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未发布子来源快照损坏时不能阻断工作区启动。

    参数：``tmp_path`` 隔离包与库存；``monkeypatch`` 注入只针对未发布子来源的
    损坏快照。返回：组合目录跳过该来源的模板投影，但 Backend 仍完成启动，父图
    保留可观察诊断。异常：若损坏的 workspace-only 快照升级为全局目录异常，
    测试会在组合入口处失败。
    """

    reset_workflow_service_for_test()
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_package(selected_root)
    publication_path = selected_root / PACKAGE_ID / "workflow_publications.json"
    publication_path.write_text(
        '{"version": 1, "publications": []}\n',
        encoding="utf-8",
    )
    original_snapshot = WorkflowStore.get_published_workflow_snapshot

    def malformed_child_snapshot(
        store: WorkflowStore,
        workflow_uuid: str,
    ) -> dict[str, object]:
        if workflow_uuid == CHILD_WORKFLOW_UUID:
            # _eligible() 会认为修订/应用摘要齐全，真正的合同投影随后发现
            # nodes 不是数组；这正是此前会抛 PublishedWorkflowGenerationError
            # 并阻断整个启动的路径。
            return {
                "workflow": {
                    "uuid": CHILD_WORKFLOW_UUID,
                    "revision": 1,
                    "workflow_type": "experiment_operation",
                    "meta_data": {
                        "unilab": {
                            "authoring_function_name": "prepare_sample",
                        }
                    },
                },
                "applied_source": {
                    "workflow_revision": 1,
                    "source_hash": "sha256:" + "a" * 64,
                },
                "source_draft_hash": "sha256:" + "b" * 64,
                "nodes": "malformed",
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            }
        return original_snapshot(store, workflow_uuid)

    monkeypatch.setattr(
        WorkflowStore,
        "get_published_workflow_snapshot",
        malformed_child_snapshot,
    )
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_package_roots=(selected_root,),
            start_source_monitor=False,
            allow_unpublished_composite_sources=True,
        )
        assert service.get_authoring(CHILD_WORKFLOW_UUID)["state"] == "applied"
        parent = service.get_authoring(PARENT_WORKFLOW_UUID)
        assert parent["state"] == "draft_invalid"
        assert parent["draft"]["diagnostics"]
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_composite_snapshot_reads_use_refreshed_device_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模板重建读取应用图时必须看到本轮设备 schema，而不是上一代。"""

    class ChangedRegistry(_RepeatRegistry):
        def obtain_registry_device_info(self) -> list[dict[str, Any]]:
            devices = super().obtain_registry_device_info()
            action = devices[1]["class"]["action_value_mappings"]["transfer"]
            action["schema"]["properties"]["goal"]["properties"]["volume"]["maximum"] = 9
            return devices

    reset_workflow_service_for_test()
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_package(selected_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        _service, projection = compose_local_workflow_template_runtime(
            tmp_path, inventory_store=inventory_store, registry=_RepeatRegistry(),
            editable_package_roots=(selected_root,), start_source_monitor=False,
            allow_unpublished_composite_sources=True,
        )
        original = WorkflowStore.get_published_workflow_snapshot
        observed = []

        def read_current_schema(
            store: WorkflowStore, workflow_uuid: str,
        ) -> dict[str, Any]:
            action = store._template_snapshot_provider.snapshot().require_action(
                "lab.devices:Pump", "transfer",
            )
            raw_schema = action.template["schema"]
            schema = json.loads(raw_schema) if isinstance(raw_schema, str) else raw_schema
            observed.append(schema["properties"]["volume"].get("maximum"))
            return original(store, workflow_uuid)

        monkeypatch.setattr(WorkflowStore, "get_published_workflow_snapshot", read_current_schema)
        projection.refresh(ChangedRegistry())
        assert observed and set(observed) == {9}
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_composite_task_persists_only_parent_workflow_authority(
    tmp_path: Path,
) -> None:
    """组合调用只建立父工作流任务（WorkflowTask）写权威。

    参数：``tmp_path`` 隔离源码包、工作流数据库与库存数据库。返回：无；断言
    应用含组合调用的父图后只持久化一个归属父工作流的任务，不为子工作流另建
    任务。异常：产品组合、应用、计划或持久化失败时由 pytest 直接报告。
    """

    reset_workflow_service_for_test()
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_package(selected_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_package_roots=(selected_root,),
        )
        parent_authoring = service.get_authoring(PARENT_WORKFLOW_UUID)
        assert parent_authoring["state"] == "applied"
        assert parent_authoring["candidate"] is None
        assert parent_authoring["draft"]["diagnostics"] == []
        parent_graph = service.get_graph(PARENT_WORKFLOW_UUID)
        assert {node["uuid"] for node in parent_graph["nodes"]} == {
            INVOCATION_UUID
        }

        task = service.create_workflow_task(
            workflow_uuid=PARENT_WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )

        page = service.list_workflow_tasks(page=1, page_size=20)
        assert page["total"] == 1
        assert [item["uuid"] for item in page["items"]] == [task["uuid"]]
        assert page["items"][0]["workflow_uuid"] == PARENT_WORKFLOW_UUID
        assert page["items"][0]["workflow_uuid"] != CHILD_WORKFLOW_UUID
        assert service.list_workflow_node_jobs(task["uuid"]) == []
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


REPEAT_CHILD_WORKFLOW_UUID = "a1000000-0000-4000-8000-000000000101"
REPEAT_PARENT_WORKFLOW_UUID = "a1000000-0000-4000-8000-000000000102"
REPEAT_INVOCATION_UUID = "a1000000-0000-4000-8000-000000000103"
REPEAT_CHILD_NODE_UUID = "a1000000-0000-4000-8000-000000000104"
REPEAT_NODE_UUID = "a1000000-0000-4000-8000-000000000105"
REPEAT_CHILD_MODULE = "c1_repeat_lab.workflows.child"
REPEAT_PACKAGE_ID = "c1_repeat_lab"
REPEAT_SOURCE_DEVICE_UUID = "30000000-0000-4000-8000-000000000001"
REPEAT_BOUND_DEVICE_UUID = "30000000-0000-4000-8000-000000000002"
REPEAT_DEVICE_TEMPLATE_UUID = device_template_uuid("pump")


def _repeat_child_source() -> str:
    """返回含 RepeatUntil 和真实注册动作的子工作流源码。"""

    return f'''from lab.devices import Pump
from unilabos.workflow.authoring import device, repeat_until, until, workflow, workflow_output


pump: Pump = device("{REPEAT_SOURCE_DEVICE_UUID}")


@workflow(
    workflow_uuid="{REPEAT_CHILD_WORKFLOW_UUID}",
    displayname="Repeat child",
    workflow_type="experiment_operation",
)
def repeat_child():
    # unilab:node_uuid={REPEAT_NODE_UUID}
    with repeat_until(max_iterations=3, carry={{}}) as loop:
        # unilab:node_uuid={REPEAT_CHILD_NODE_UUID}
        accepted = pump.transfer(volume=1.5)
        until(accepted.accepted)
    return workflow_output()
'''


def _repeat_parent_source() -> str:
    """返回不含组合调用、供真实 API 插入的父工作流源码。"""

    return f'''from unilabos.workflow.authoring import workflow, workflow_output


@workflow(workflow_uuid="{REPEAT_PARENT_WORKFLOW_UUID}", displayname="Repeat parent")
def repeat_parent():
    return workflow_output()
'''


def _write_repeat_package(selected_root: Path) -> None:
    """写入 RepeatUntil 子/父工作流和显式包清单。"""

    source_path = selected_root / REPEAT_PACKAGE_ID / "workflows" / "child.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_repeat_child_source(), encoding="utf-8")
    (selected_root / REPEAT_PACKAGE_ID / "workflows" / "parent.py").write_text(
        _repeat_parent_source(), encoding="utf-8"
    )
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        f"  name: {REPEAT_PACKAGE_ID}\n"
        "workflows:\n"
        f"  - workflow_uuid: {REPEAT_CHILD_WORKFLOW_UUID}\n"
        f"    source: {REPEAT_PACKAGE_ID}/workflows/child.py\n"
        f"  - workflow_uuid: {REPEAT_PARENT_WORKFLOW_UUID}\n"
        f"    source: {REPEAT_PACKAGE_ID}/workflows/parent.py\n",
        encoding="utf-8",
    )


class _RepeatRegistry(FakeRegistry):
    """为源托管组合回归测试提供带 Pump 动作的注册表。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """同时提供组合宿主节点和 Pump 动作模板。"""

        return [
            *_Registry().obtain_registry_device_info(),
            *super().obtain_registry_device_info(),
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """沿用基础注册表的独立资源定义，避免业务 ID 重复。"""

        return _Registry().obtain_registry_resource_info()


def test_source_backed_repeat_composition_preserves_parent_device_binding(
    tmp_path: Path,
) -> None:
    """API 组合 RepeatUntil 后，父源码固定点必须保留实际设备绑定。"""

    reset_workflow_service_for_test()
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_repeat_package(selected_root)
    inventory_store = InventoryStore()
    try:
        with InventoryStore.transaction(inventory_store) as transaction:
            transaction.execute(
                """
                INSERT INTO resource_template(
                    uuid, create_time, update_time, meta_data,
                    name, display_name, resource_type, module, aggregate_version
                ) VALUES (?,?,?,?,?,?,?,?,1)
                """,
                (
                    REPEAT_DEVICE_TEMPLATE_UUID,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                    "{}",
                    "pump",
                    "注射泵动作",
                    "device",
                    "lab.devices:Pump",
                ),
            )
            transaction.execute(
                """
                INSERT INTO material(
                    uuid, create_time, update_time, meta_data,
                    resource_template_uuid, class, barcode, name, aggregate_version
                ) VALUES (?,?,?,?,?,?,?,?,1)
                """,
                (
                    REPEAT_SOURCE_DEVICE_UUID,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                    '{"edge_local_id":"pump-01"}',
                    REPEAT_DEVICE_TEMPLATE_UUID,
                    "device",
                    "",
                    "pump-01",
                ),
            )
            transaction.execute(
                """
                INSERT INTO material(
                    uuid, create_time, update_time, meta_data,
                    resource_template_uuid, class, barcode, name, aggregate_version
                ) VALUES (?,?,?,?,?,?,?,?,1)
                """,
                (
                    REPEAT_BOUND_DEVICE_UUID,
                    "2026-01-01T00:00:00.000Z",
                    "2026-01-01T00:00:00.000Z",
                    '{"edge_local_id":"pump-02"}',
                    REPEAT_DEVICE_TEMPLATE_UUID,
                    "device",
                    "",
                    "pump-02",
                ),
            )
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RepeatRegistry(),
            editable_package_roots=(selected_root,),
            start_source_monitor=False,
        )
        child_graph = service.get_graph(REPEAT_CHILD_WORKFLOW_UUID)
        assert {node["type"] for node in child_graph["nodes"]} >= {
            "repeat_until",
            "ILab",
        }, service.get_authoring(REPEAT_CHILD_WORKFLOW_UUID)
        child_contract = service.publish_workflow_contract(
            REPEAT_CHILD_WORKFLOW_UUID,
            revision=child_graph["workflow"]["revision"],
        )
        result = service.insert_composite_workflow(
            REPEAT_PARENT_WORKFLOW_UUID,
            revision=service.get_graph(REPEAT_PARENT_WORKFLOW_UUID)["workflow"]["revision"],
            contract_uuid=child_contract["uuid"],
            invocation_uuid=REPEAT_INVOCATION_UUID,
            device_bindings={"executor_1": REPEAT_BOUND_DEVICE_UUID},
            pose={"x": 0, "y": 0},
            param={},
        )
        nodes = {str(node["uuid"]): node for node in result["nodes"]}
        assert nodes[REPEAT_INVOCATION_UUID]["type"] == "workflow"
        bound_actions = [
            node
            for node in result["nodes"]
            if node.get("type") == "ILab"
            and node.get("material_uuid") == REPEAT_BOUND_DEVICE_UUID
        ]
        assert len(bound_actions) == 1
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()
