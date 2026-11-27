"""Workspace Release 对 MaterialSource 监管策略的发布回归。"""

from unilabos.workspace_host.release_publish import _bind_backend_material_sources


def _source_node(*, node_uuid: str, custody_policy: str) -> dict:
    """构造最小物料来源节点；参数是节点身份与监管策略，返回发布测试输入。"""
    return {
        "uuid": node_uuid,
        "type": "material_source",
        "param": {
            "mode": "existing",
            "material_uuid": None,
            "resource_template_uuid": "template-local",
            "mount": {"uuid": "mount-local"},
            "site": None,
            "slot_range": None,
            "flow_role": "primary_sample",
            "custody_policy": custody_policy,
        },
    }


def _bind(*, custody_policy: str) -> dict:
    """按给定监管策略发布最小图，返回已绑定的 Backend 工作流图。"""
    graph = {
        "workflow": {"uuid": "workflow-local", "name": "source"},
        "nodes": [
            _source_node(
                node_uuid=f"source-{custody_policy}",
                custody_policy=custody_policy,
            )
        ],
    }
    material_graph = {
        "nodes": [
            {
                "material": {
                    "uuid": "mount-local",
                    "resource_template_uuid": "mount-template-local",
                },
                "sites": [{"uuid": "site-1", "name": "L1", "sort_order": 1}],
            },
            {
                "material": {
                    "uuid": "material-1",
                    "resource_template_uuid": "template-local",
                    "parent_uuid": "mount-local",
                },
                "current_site_uuid": "site-1",
                "sites": [],
            },
        ]
    }
    return _bind_backend_material_sources(
        graph,
        material_graph=material_graph,
        material_identities={
            "mount-local": "mount-target",
            "material-1": "material-target-1",
        },
        material_template_names={
            "mount-local": "mount-template",
            "material-1": "derived-template-1",
        },
        source_template_names={"template-local": "canonical-template"},
        target_templates={
            "mount-template": "mount-template-target",
            "derived-template-1": "derived-template-target-1",
            "canonical-template": "canonical-template-target",
        },
    )


def test_unbound_task_exclusive_source_stays_dynamic_for_parallel_tasks() -> None:
    """未指定实例的任务独占来源必须交给 Backend 在每个 Task 准入时选择。"""
    bound = _bind(custody_policy="task_exclusive")

    param = bound["nodes"][0]["param"]
    assert param["material_uuid"] is None
    assert param["resource_template_uuid"] == "derived-template-target-1"


def test_unbound_shared_source_is_frozen_to_one_shared_material() -> None:
    """共享来源需冻结同一物料身份，让多个 Task 共享其动作期 Lease。"""
    bound = _bind(custody_policy="shared_source")

    param = bound["nodes"][0]["param"]
    assert param["material_uuid"] == "material-target-1"
    assert param["resource_template_uuid"] == "derived-template-target-1"
