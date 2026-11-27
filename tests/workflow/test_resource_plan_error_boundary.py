"""延迟库位资源计划的序列化错误遵守创作与运行验证边界。"""

from typing import Any

import pytest

from tests.workflow.test_f08_single_node_execution_plan import _http_node
from unilabos.workflow.execution_plan import ExecutionPlanBuilder, ExecutionPlanBuildError
from unilabos.workflow.graph_executability import assert_graph_executable
from unilabos.workflow.resource_lock_plan import ResourcePlanError
from unilabos.workflow.run_preflight import build_run_preflight_report


def _deferred_site_cycle() -> dict[str, Any]:
    """独立入口分别持有库位和机器人，共同入口再请求另一资源。"""

    common, site_only, robot_only = [
        f"20000000-0000-4000-8000-00000000000{index}" for index in (1, 2, 3)
    ]
    return {
        "workflow": {"uuid": "10000000-0000-4000-8000-000000000001", "revision": 1},
        "nodes": [_http_node(identity) for identity in (common, site_only, robot_only)],
        "edges": [],
        # 库位身份尚待库存权威绑定，模板阶段的环检查推迟到序列化。
        "resource_bindings": {},
        "resource_scopes": [
            {"scope_id": "site", "kind": "reservation", "resources": ["site:deck/one"],
             "node_uuids": [common, site_only]},
            {"scope_id": "robot", "kind": "transfer", "resources": ["robot"],
             "node_uuids": [common, robot_only]},
        ],
    }


@pytest.mark.parametrize("refresh", [False, True])
def test_static_build_and_refresh_normalize_serialization_cycle(refresh: bool) -> None:
    graph = _deferred_site_cycle()
    builder = ExecutionPlanBuilder()
    if refresh:
        unreserved = {**graph, "resource_scopes": []}
        plan, jobs = builder.build(unreserved, run_mode="normal", target_node_uuid=None)

    with pytest.raises(ExecutionPlanBuildError) as caught:
        if refresh:
            builder.refresh_resource_plan(graph=graph, plan=plan, jobs=jobs)
        else:
            builder.build(graph, run_mode="normal", target_node_uuid=None)

    assert caught.value.code == "invalid_resource_plan"
    assert "成环" in str(caught.value) and "/relations" in str(caught.value)
    assert isinstance(caught.value.__cause__, ResourcePlanError)
    assert caught.value.__cause__.code == "resource_cycle"


def test_authoring_does_not_reject_runtime_resource_policy() -> None:
    graph = _deferred_site_cycle()
    assert_graph_executable(graph, workflow_uuid=graph["workflow"]["uuid"])


def test_static_preflight_reports_cycle_instead_of_raising() -> None:
    report = build_run_preflight_report(
        graph=_deferred_site_cycle(), run_mode="normal", target_node_uuid=None,
        material_resolver=None,
    )
    assert not report["can_run"]
    assert any(check["blocking"] and "成环" in check["message"] for check in report["checks"])
