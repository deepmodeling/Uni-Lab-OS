"""连续发布须经过真实源码回写、重新编译和冷启动后仍能更新父流程。"""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from tests.registry.test_template_projection import (
    DEVICE_MATERIAL_UUID,
    FakeInventoryStore,
)
from tests.workflow.test_composite_auto_refresh import _apply_source
from tests.workflow.test_published_composite_restart import (
    _child_source,
    _parent_source,
    _RegistryWithPump,
    _write_package_root,
)
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.service import WorkflowService

CHILD_UUID = "60000000-0000-4000-8000-000000000001"
PARENT_UUID = "60000000-0000-4000-8000-000000000002"
INVOCATION_UUID = "4fcacda7-dca4-4db0-a972-8d50d8fce6af"


def _source_with_steps(count: int) -> str:
    """保留已有动作身份，每次只在末尾增加一个动作。"""
    actions = "\n".join(
        f"    # unilab:node_uuid={uuid5(NAMESPACE_URL, f'publication-step-{index}')}\n"
        f"    step_{index} = pump.prime()"
        for index in range(count)
    )
    declaration = _child_source(CHILD_UUID).replace(
        "device()", f'device("{DEVICE_MATERIAL_UUID}")'
    )
    return declaration.split("def child():")[0] + (
        f"def child():\n{actions}\n    return workflow_output()\n"
    )


def _pin(graph: dict) -> dict:
    """取出本次调用的发布合同信息。"""
    return next(node for node in graph["nodes"] if node["uuid"] == INVOCATION_UUID)[
        "meta_data"
    ]["unilab"]["composite"]


@pytest.mark.parametrize("parent_source", ["api", "python"])
@pytest.mark.parametrize("publish_parent", [False, True])
def test_consecutive_publications_keep_identity_through_source_and_restart(
    tmp_path: Path,
    parent_source: str,
    publish_parent: bool,
) -> None:
    """只保存不刷新；连续发布、父流程发布和重启后都能继续刷新，旧任务不变。"""
    reset_workflow_service_for_test()
    package_root = tmp_path / "editable"
    _write_package_root(package_root)
    inventory = FakeInventoryStore()

    def start() -> WorkflowService:
        """使用同一领域包重新建立真实定义目录，不复用旧服务内存。"""
        return compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory,
            registry=_RegistryWithPump(),
            editable_package_roots=(package_root,),
            start_source_monitor=False,
        )[0]

    try:
        service = start()
        service.import_python_workflow(
            file_name="child.py", python_source=_source_with_steps(1)
        )
        published = service.publish_workflow_contract(CHILD_UUID, revision=1)
        if parent_source == "api":
            service.create_workflow(
                workflow_uuid=PARENT_UUID,
                name="连续发布父流程",
                tags=[],
                description=None,
                meta_data={},
            )
            service.insert_composite_workflow(
                PARENT_UUID,
                revision=1,
                contract_uuid=published["uuid"],
                invocation_uuid=INVOCATION_UUID,
                pose={"x": 180, "y": 160},
                param={},
                device_bindings={
                    item["key"]: DEVICE_MATERIAL_UUID
                    for item in published["executor_requirements"]
                },
            )
        else:
            service.import_python_workflow(
                file_name="parent.py", python_source=_parent_source(PARENT_UUID)
            )
        graph = service.get_graph(PARENT_UUID)
        assert _pin(graph).get("contract_uuid") == published["uuid"]
        frozen = None
        if parent_source == "api":
            frozen = service.create_workflow_task(
                workflow_uuid=PARENT_UUID,
                run_mode="normal",
                target_node_uuid=None,
                input_value={},
                description=None,
                meta_data={},
            )

        for count in (2, 3, 4):
            if count == 4:
                reset_workflow_service_for_test()
                service = start()
                graph = service.get_graph(PARENT_UUID)
                assert len(graph["nodes"]) == 4
                assert _pin(graph).get("contract_uuid") == published["uuid"]
            before = service.get_graph(PARENT_UUID)
            _apply_source(service, CHILD_UUID, _source_with_steps(count))
            unpublished = service.get_graph(PARENT_UUID)
            assert unpublished["nodes"] == before["nodes"]
            assert unpublished["edges"] == before["edges"]
            published = service.publish_workflow_contract(
                CHILD_UUID,
                revision=service.get_workflow(CHILD_UUID)["revision"],
            )
            refresh = published.get("dependent_refresh", {})
            assert refresh.get("pending") == [], refresh
            assert PARENT_UUID in refresh["updated_workflow_uuids"]
            graph = service.get_graph(PARENT_UUID)
            assert len(graph["nodes"]) == count + 1
            assert _pin(graph).get("contract_uuid") == published["uuid"]
            assert (
                _pin(graph)["child_workflow_revision"] == published["workflow_revision"]
            )
            assert _pin(graph)["device_bindings"] == {
                "executor_1": DEVICE_MATERIAL_UUID
            }
            assert all(
                node["material_uuid"] == DEVICE_MATERIAL_UUID
                for node in graph["nodes"] if node["type"] == "ILab"
            )
            if frozen is not None:
                assert (
                    service.get_workflow_task(frozen["uuid"])["workflow_snapshot"]
                    == frozen["workflow_snapshot"]
                )
            # 原始复现中父流程在子流程的两次发布之间也发布过。
            if publish_parent:
                service.publish_workflow_contract(
                    PARENT_UUID, revision=graph["workflow"]["revision"]
                )
    finally:
        reset_workflow_service_for_test()
        inventory.close()


@pytest.mark.parametrize(
    "mismatch", ["workflow_uuid", "workflow_revision", "contract_uuid"]
)
def test_mismatched_publication_identity_is_not_attached_to_invocation(
    mismatch: str,
) -> None:
    """恢复发布身份必须核对来源与修订，不能把其他合同编号填到调用根。"""
    from tests.workflow.test_c1_r2_static_expansion_contract import (
        CHILD_TEMPLATE_UUID,
        CHILD_WORKFLOW_UUID,
        PARENT_WORKFLOW_UUID,
        _world_components,
    )
    from tests.workflow.test_c1_r2_static_expansion_contract import (
        INVOCATION_UUID as CALL_UUID,
    )
    from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
    from unilabos.workflow.composite import CompositeAuthoring

    _, provider, catalog, resolver = _world_components()
    templates = [action.detached_template() for action in catalog.actions]
    handles = [
        handle for action in catalog.actions for handle in action.detached_handles()
    ]
    template = next(item for item in templates if item["uuid"] == CHILD_TEMPLATE_UUID)
    publication = {
        "contract_uuid": "60000000-0000-4000-8000-000000000010",
        "workflow_uuid": CHILD_WORKFLOW_UUID,
        "workflow_revision": 7,
    }
    publication[mismatch] = 8 if mismatch == "workflow_revision" else "invalid-identity"
    template.setdefault("meta_data", {}).setdefault("unilab", {})[
        "workflow_contract"
    ] = publication
    authoring = CompositeAuthoring(
        snapshot_provider=provider,
        catalog=AuthoringCatalogSnapshot.from_entities(templates, handles),
        resolver=resolver,
    )
    expansion = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=CALL_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": 7.5},
    )
    assert expansion.invocation_node is None
    assert expansion.nodes == ()
    assert {item["code"] for item in expansion.diagnostics} == {
        "composite_catalog_mismatch"
    }
