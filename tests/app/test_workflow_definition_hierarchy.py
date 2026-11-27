"""定义接口保留组合边界；完整执行图、发布和源码回写不丢子步骤。"""

import pytest
from fastapi.testclient import TestClient

from tests.registry.test_template_projection import FakeInventoryStore
from tests.workflow.test_composite_publication_roundtrip import (
    CHILD_UUID,
    INVOCATION_UUID,
    PARENT_UUID,
    _source_with_steps,
)
from tests.workflow.test_published_composite_restart import (
    _parent_source,
    _RegistryWithPump,
    _write_package_root,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)


@pytest.fixture
def hierarchy(tmp_path):
    reset_workflow_service_for_test()
    root = tmp_path / "editable"
    _write_package_root(root)
    service = compose_local_workflow_template_runtime(
        tmp_path,
        inventory_store=FakeInventoryStore(),
        registry=_RegistryWithPump(),
        editable_package_roots=(root,),
        start_source_monitor=False,
    )[0]
    service.import_python_workflow(
        file_name="child.py", python_source=_source_with_steps(2)
    )
    service.publish_workflow_contract(CHILD_UUID, revision=1)
    service.import_python_workflow(
        file_name="parent.py", python_source=_parent_source(PARENT_UUID)
    )
    try:
        yield service, TestClient(create_workflow_app(service))
    finally:
        reset_workflow_service_for_test()


def test_definition_read_keeps_child_reference_and_task_keeps_both_actions(hierarchy):
    service, client = hierarchy
    full = service.get_graph(PARENT_UUID)
    assert len(full["nodes"]) == 3
    shown = client.get(f"/api/v1/workflows/{PARENT_UUID}/graph").json()["data"]
    assert [node["uuid"] for node in shown["nodes"]] == [INVOCATION_UUID]
    assert (
        shown["nodes"][0]["meta_data"]["unilab"]["composite"]["child_workflow_uuid"]
        == CHILD_UUID
    )
    listed = client.get(f"/api/v1/workflows/{PARENT_UUID}/nodes").json()["data"]
    assert listed["total"] == 1
    child = client.get(f"/api/v1/workflows/{CHILD_UUID}/graph").json()["data"]
    assert len(child["nodes"]) == 2
    task = service.create_workflow_task(
        workflow_uuid=PARENT_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    assert len(task["workflow_snapshot"]["nodes"]) == 3
    assert sum(n["type"] == "ILab" for n in task["workflow_snapshot"]["nodes"]) == 2
    assert service.get_graph(PARENT_UUID) == full


def test_compact_graph_save_preserves_hidden_steps_and_source_calls(hierarchy):
    service, client = hierarchy
    shown = client.get(f"/api/v1/workflows/{PARENT_UUID}/graph").json()["data"]
    assert len(shown["nodes"]) == 1
    shown["nodes"][0]["pose"] = {"x": 71, "y": 93}
    response = client.put(
        f"/api/v1/workflows/{PARENT_UUID}/graph",
        json={
            "revision": shown["workflow"]["revision"],
            "nodes": shown["nodes"],
            "edges": shown["edges"],
        },
    ).json()
    assert response["code"] == 0, response
    assert len(response["data"]["nodes"]) == 1
    assert len(service.get_graph(PARENT_UUID)["nodes"]) == 3
    source = service.get_authoring(PARENT_UUID)["draft"]["python_source"]
    assert "result = child()" in source
    assert "pump.prime" not in source


def test_removing_child_reference_removes_its_internal_steps(hierarchy):
    service, client = hierarchy
    current = service.get_graph(PARENT_UUID)
    response = client.put(
        f"/api/v1/workflows/{PARENT_UUID}/graph",
        json={
            "revision": current["workflow"]["revision"],
            "nodes": [],
            "edges": [],
        },
    ).json()
    assert response["code"] == 0, response
    assert service.get_graph(PARENT_UUID)["nodes"] == []
