"""微后端 CompositeWorkflowInvocation 的发布、展开与执行计划回归。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unilabos.server.services.runtime.workflow.authoring_identity import expanded_node_uuid
from unilabos.server.services.runtime.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.server.services.runtime.workflow.catalog import PublishedSourceCatalog
from unilabos.server.services.runtime.workflow.composite import (
    CompositeAuthoring,
    project_published_workflow_contract,
)
from unilabos.server.services.runtime.workflow.composite_compatibility import (
    classify_published_workflow_compatibility_projections,
    published_workflow_compatibility_projection,
)
from unilabos.server.services.runtime.workflow.service import WorkflowService

PARENT_UUID = "10000000-0000-4000-8000-000000000001"
CHILD_UUID = "10000000-0000-4000-8000-000000000002"
INVOCATION_UUID = "10000000-0000-4000-8000-000000000003"
CHILD_NODE_UUID = "10000000-0000-4000-8000-000000000004"
ACTION_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000001"
ACTION_RESOURCE_UUID = "20000000-0000-4000-8000-000000000002"
HOST_RESOURCE_UUID = "20000000-0000-4000-8000-000000000003"
ACTION_TARGET_UUID = "30000000-0000-4000-8000-000000000001"
ACTION_SOURCE_UUID = "30000000-0000-4000-8000-000000000002"
ACTION_READY_TARGET_UUID = "30000000-0000-4000-8000-000000000003"
ACTION_READY_SOURCE_UUID = "30000000-0000-4000-8000-000000000004"
MATERIAL_TEMPLATE_A = "40000000-0000-4000-8000-000000000001"
MATERIAL_TEMPLATE_B = "40000000-0000-4000-8000-000000000002"
APPLIED_HASH = "sha256:" + "a" * 64


def _slot(template_uuid: str) -> dict[str, Any]:
    return {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [template_uuid],
    }


def _action_template() -> dict[str, Any]:
    return {
        "uuid": ACTION_TEMPLATE_UUID,
        "resource_template_uuid": ACTION_RESOURCE_UUID,
        "name": "consume",
        "display_name": "Consume",
        "description": "fixture",
        "class": "fixture.devices:Device",
        "goal": {"material": "material"},
        "goal_default": {},
        "feedback": {},
        "result": {"material": "material"},
        "schema": None,
        "type": "action",
        "node_type": "device",
        "meta_data": {},
    }


def _handle(
    uuid: str,
    key: str,
    io_type: str,
    *,
    ready: bool = False,
) -> dict[str, Any]:
    schema = {"type": "object"} if ready else _slot(MATERIAL_TEMPLATE_A)
    return {
        "uuid": uuid,
        "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
        "handle_key": key,
        "io_type": io_type,
        "display_name": key,
        "description": "",
        "type": "default" if ready else "ResourceSlot",
        "required": io_type == "target" and not ready,
        "data_source": None if ready else "executor",
        "data_key": None if ready else "material",
        "meta_data": {} if ready else {"unilab": {"value_schema": schema}},
    }


def _action_handles() -> list[dict[str, Any]]:
    return [
        _handle(ACTION_TARGET_UUID, "material", "target"),
        _handle(ACTION_SOURCE_UUID, "material", "source"),
        _handle(ACTION_READY_TARGET_UUID, "ready", "target", ready=True),
        _handle(ACTION_READY_SOURCE_UUID, "ready", "source", ready=True),
    ]


def _snapshot() -> dict[str, Any]:
    return {
        "workflow": {
            "uuid": CHILD_UUID,
            "revision": 3,
            "name": "Child",
            "tags": [],
            "description": "fixture",
            "meta_data": {
                "unilab": {
                    "authoring_function_name": "child",
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "material",
                                "schema": _slot(MATERIAL_TEMPLATE_A),
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {
                        "version": 1,
                        "outputs": [
                            {
                                "name": "material",
                                "schema": _slot(MATERIAL_TEMPLATE_A),
                                "implicit": False,
                            }
                        ],
                    },
                    "output_bindings": {
                        "material": {
                            "kind": "node_output",
                            "workflow_node_uuid": CHILD_NODE_UUID,
                            "source_handle_uuid": ACTION_SOURCE_UUID,
                        }
                    },
                }
            },
        },
        "applied_source": {
            "workflow_revision": 3,
            "source_hash": APPLIED_HASH,
            "python_source": "def child(*, material): ...\n",
            "source_map": [],
            "compiler_version": "fixture",
            "template_catalog_fingerprint": "sha256:" + "b" * 64,
        },
        "nodes": [
            {
                "uuid": CHILD_NODE_UUID,
                "workflow_uuid": CHILD_UUID,
                "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                "parent_uuid": None,
                "name": "consume",
                "status": "idle",
                "type": "device",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            ACTION_TARGET_UUID: {"parameter": "material"}
                        }
                    }
                },
            }
        ],
        "edges": [],
        "node_templates": [_action_template()],
        "handle_templates": _action_handles(),
    }


def _source_catalog() -> PublishedSourceCatalog:
    return PublishedSourceCatalog.from_records(
        [
            {
                "workflow_uuid": CHILD_UUID,
                "definition_fqid": "fixture.workflows.child",
                "module": "fixture.workflows",
                "symbol": "child",
                "source_uri": "package://fixture/workflows/child.py",
                "definition_content_hash": "sha256:" + "c" * 64,
            }
        ]
    )


@dataclass
class _Provider:
    snapshots: dict[str, dict[str, Any]]

    def get_published_workflow_snapshot(self, workflow_uuid: str) -> dict[str, Any]:
        try:
            return self.snapshots[workflow_uuid]
        except KeyError:
            raise LookupError(workflow_uuid) from None


def _world() -> tuple[CompositeAuthoring, dict[str, Any], list[dict[str, Any]]]:
    sources = _source_catalog()
    snapshot = _snapshot()
    contract = project_published_workflow_contract(
        source=sources.resolve("fixture.workflows", "child"),
        applied_snapshot=snapshot,
        host_node_resource_template={
            "uuid": HOST_RESOURCE_UUID,
            "name": "host_node",
            "display_name": "Host Node",
        },
    )
    assert contract is not None
    catalog = AuthoringCatalogSnapshot.from_entities(
        [_action_template(), contract.template],
        [*_action_handles(), *contract.handles],
    )
    return (
        CompositeAuthoring(
            snapshot_provider=_Provider({CHILD_UUID: snapshot}),
            catalog=catalog,
            resolver=sources,
        ),
        contract.template,
        list(contract.handles),
    )


def test_template_uuid_allowlist_is_frontend_hint_not_backend_gate() -> None:
    authoring, template, handles = _world()
    expansion = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="fixture.workflows",
        symbol="child",
        keyword_arguments={
            "material": {"kind": "workflow_input", "parameter": "sample"}
        },
        parent_input_contract={
            "version": 1,
            "parameters": [
                {
                    "name": "sample",
                    "schema": _slot(MATERIAL_TEMPLATE_B),
                    "required": True,
                }
            ],
        },
    )

    assert expansion.diagnostics == ()
    assert expansion.effective_parent_input_contract["parameters"][0]["schema"] == (
        _slot(MATERIAL_TEMPLATE_B)
    )
    assert expansion.nodes[0]["uuid"] == expanded_node_uuid(
        INVOCATION_UUID,
        CHILD_NODE_UUID,
    )

    previous = published_workflow_compatibility_projection(template, handles)
    changed = {**previous, "inputs": [dict(previous["inputs"][0])]}
    changed["inputs"][0]["schema"] = _slot(MATERIAL_TEMPLATE_B)
    assert (
        classify_published_workflow_compatibility_projections(previous, changed)
        == "exact"
    )


def test_recursive_and_missing_child_fail_without_partial_graph() -> None:
    authoring, _template, _handles = _world()
    recursive = authoring.compile_invocation(
        parent_workflow_uuid=CHILD_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="fixture.workflows",
        symbol="child",
        keyword_arguments={"material": {"uuid": PARENT_UUID}},
    )
    missing = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="fixture.unknown",
        symbol="child",
        keyword_arguments={},
    )

    assert recursive.nodes == ()
    assert recursive.diagnostics[0]["code"] == "composite_recursive_reference"
    assert missing.nodes == ()
    assert missing.diagnostics[0]["code"] == "composite_child_not_found"


def test_composite_display_node_does_not_generate_job() -> None:
    authoring, template, handles = _world()
    expansion = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="fixture.workflows",
        symbol="child",
        keyword_arguments={"material": {"uuid": PARENT_UUID}},
    )
    assert expansion.invocation_node is not None
    internal = dict(expansion.nodes[0])
    producer_uuid = "50000000-0000-4000-8000-000000000001"
    consumer_uuid = "50000000-0000-4000-8000-000000000002"
    producer = {
        **internal,
        "uuid": producer_uuid,
        "parent_uuid": None,
        "meta_data": {"unilab": {"input_bindings": {}}},
    }
    consumer = {
        **internal,
        "uuid": consumer_uuid,
        "parent_uuid": None,
        "meta_data": {"unilab": {"input_bindings": {}}},
    }
    workflow_target = next(
        item["uuid"]
        for item in handles
        if item["io_type"] == "target" and item["handle_key"] == "material"
    )
    workflow_source = next(
        item["uuid"]
        for item in handles
        if item["io_type"] == "source" and item["handle_key"] == "material"
    )
    graph = {
        "workflow": {"uuid": PARENT_UUID, "revision": 1, "meta_data": {}},
        "nodes": [producer, dict(expansion.invocation_node), internal, consumer],
        "edges": [
            {
                "uuid": "60000000-0000-4000-8000-000000000001",
                "source_node_uuid": producer_uuid,
                "source_handle_uuid": ACTION_SOURCE_UUID,
                "target_node_uuid": INVOCATION_UUID,
                "target_handle_uuid": workflow_target,
            },
            {
                "uuid": "60000000-0000-4000-8000-000000000002",
                "source_node_uuid": INVOCATION_UUID,
                "source_handle_uuid": workflow_source,
                "target_node_uuid": consumer_uuid,
                "target_handle_uuid": ACTION_TARGET_UUID,
            },
        ],
        "node_templates": [_action_template(), template],
        "handle_templates": [*_action_handles(), *handles],
    }
    service = WorkflowService(":memory:")
    try:
        plan, jobs = service._build_execution_plan(  # noqa: SLF001
            graph,
            run_mode="normal",
            target_node_uuid=None,
        )
    finally:
        service.close()

    assert INVOCATION_UUID not in {item["uuid"] for item in plan["nodes"]}
    assert INVOCATION_UUID not in {item["workflow_node_uuid"] for item in jobs}
    assert {item["workflow_node_uuid"] for item in jobs} == {
        producer_uuid,
        internal["uuid"],
        consumer_uuid,
    }
    assert {
        (item["source_node_uuid"], item["target_node_uuid"])
        for item in plan["edges"]
    } == {
        (producer_uuid, internal["uuid"]),
        (internal["uuid"], consumer_uuid),
    }


def test_store_returns_graph_and_applied_source_in_one_snapshot() -> None:
    from unilabos.server.services.runtime.workflow.store import WorkflowStore

    store = WorkflowStore(":memory:")
    try:
        store.create_workflow(
            workflow_uuid=CHILD_UUID,
            name="Child",
            tags=[],
            description=None,
            meta_data={},
        )
        with store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflow_authoring(
                    workflow_uuid, observed_draft_hash, draft_update_time,
                    diagnostics, candidate_hash, candidate, applied_source,
                    writeback_status, writeback_source,
                    writeback_expected_hash, writeback_generation, update_time
                ) VALUES (?, NULL, NULL, '[]', NULL, NULL, ?, 'settled',
                          NULL, NULL, NULL, '2026-08-23T00:00:00Z')
                """,
                (
                    CHILD_UUID,
                    '{"workflow_revision":1,"source_hash":"' + APPLIED_HASH + '"}',
                ),
            )
        snapshot = store.get_published_workflow_snapshot(CHILD_UUID)
    finally:
        store.close()

    assert snapshot["workflow"]["uuid"] == CHILD_UUID
    assert snapshot["applied_source"]["source_hash"] == APPLIED_HASH


class _FixedPointStore:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {
            PARENT_UUID: {"candidate": None},
            CHILD_UUID: {"candidate": _candidate(CHILD_UUID)},
        }

    def get_authoring_record(self, workflow_uuid: str) -> dict[str, Any]:
        return self.records[workflow_uuid]


def _candidate(seed: str) -> dict[str, Any]:
    return {
        "candidate_hash": "sha256:" + "d" * 64,
        "draft_hash": "sha256:" + "e" * 64,
        "base_workflow_revision": 1,
        "seed": seed,
    }


def test_cold_activation_advances_child_before_parent() -> None:
    store = _FixedPointStore()
    service = WorkflowService(":memory:")
    service.get_authoring_record = store.get_authoring_record  # type: ignore[method-assign]
    applied: list[str] = []
    service.list_registered_sources = lambda: [  # type: ignore[method-assign]
        {"workflow_uuid": PARENT_UUID},
        {"workflow_uuid": CHILD_UUID},
    ]
    service.recover_registered_sources = lambda **_kwargs: None  # type: ignore[method-assign]

    def apply(workflow_uuid: str, **_kwargs: Any) -> dict[str, Any]:
        applied.append(workflow_uuid)
        if workflow_uuid == CHILD_UUID:
            store.records[PARENT_UUID] = {"candidate": _candidate(PARENT_UUID)}
        return {"apply_result": {"warnings": []}}

    service.apply_authoring = apply  # type: ignore[method-assign]
    service.activate_registered_sources_to_fixed_point()

    assert applied == [CHILD_UUID, PARENT_UUID]
