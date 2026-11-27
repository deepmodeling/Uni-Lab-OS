"""目录依赖创作刷新深模块（Deep Module）的公开行为合同。"""

from __future__ import annotations

from unittest.mock import Mock, call

from unilabos.workflow.catalog_dependent_authoring_refresh import (
    refresh_catalog_dependent_authoring,
)


def test_clean_parent_is_applied_while_dirty_parent_remains_for_user() -> None:
    """自动应用干净父工作流，并保留已有待应用草稿。

    参数：无。
    返回：无；断言两个直接父工作流都按新目录重新编译，但只有刷新前没有候选、
    诊断或写回故障的父工作流自动应用，已有用户草稿的父工作流只留下警告。
    异常：筛选、自动应用或用户草稿保护合同漂移时由 pytest 报告。
    """

    # 三个 UUID 分别代表刚变更来源、干净父来源和持有旧候选的父来源。
    mutated_workflow_uuid = "11111111-1111-4111-8111-111111111111"
    clean_workflow_uuid = "22222222-2222-4222-8222-222222222222"
    dirty_workflow_uuid = "33333333-3333-4333-8333-333333333333"
    authoring_before_refresh = {
        clean_workflow_uuid: {
            "state": "applied",
            "candidate": None,
        },
        dirty_workflow_uuid: {
            "state": "applied_source_stale",
            "candidate": None,
        },
    }
    authoring_loader = Mock(side_effect=authoring_before_refresh.__getitem__)
    authorings = {
        clean_workflow_uuid: {
            "state": "unapplied_graph",
            "candidate": {"candidate_hash": "new-clean"},
        },
        dirty_workflow_uuid: {
            "state": "unapplied_graph",
            "candidate": {"candidate_hash": "new-dirty"},
        },
    }
    reconcile_callback = Mock(side_effect=authorings.__getitem__)
    apply_callback = Mock(
        return_value={"apply_result": {"warnings": []}},
    )
    warnings: list[dict[str, str]] = []

    refresh_catalog_dependent_authoring(
        dependent_workflow_uuids=(
            clean_workflow_uuid,
            dirty_workflow_uuid,
            dirty_workflow_uuid,
        ),
        load_authoring=authoring_loader,
        reconcile_source=reconcile_callback,
        apply_candidate=apply_callback,
        warnings=warnings,
        mutated_workflow_uuid=mutated_workflow_uuid,
    )

    assert authoring_loader.call_args_list == [
        call(clean_workflow_uuid),
        call(dirty_workflow_uuid),
    ]
    assert reconcile_callback.call_args_list == [
        call(clean_workflow_uuid),
        call(dirty_workflow_uuid),
    ]
    apply_callback.assert_called_once_with(
        clean_workflow_uuid,
        candidate_hash="new-clean",
    )
    assert warnings == [
        {
            "code": "dependent_authoring_refresh_pending",
            "message": (
                f"实验操作已更新，但引用方工作流 {dirty_workflow_uuid} 未能自动更新；"
                "请打开该工作流，检查组合节点参数和设备动作模板，重新编译并应用"
            ),
        }
    ]


def test_refresh_failure_becomes_post_commit_warning() -> None:
    """父工作流重编译失败只形成可观察警告。

    参数：无。返回：无；断言异常不越过已经完成的实验操作提交边界，且警告携带
    具体父工作流身份。异常：故障隔离合同漂移时由 pytest 报告。
    """

    child_uuid = "11111111-1111-4111-8111-111111111111"
    parent_uuid = "22222222-2222-4222-8222-222222222222"
    warnings: list[dict[str, str]] = []

    refresh_catalog_dependent_authoring(
        dependent_workflow_uuids=(parent_uuid,),
        load_authoring=lambda _workflow_uuid: {
            "state": "applied",
            "candidate": None,
        },
        reconcile_source=Mock(side_effect=RuntimeError("目录暂不可用")),
        apply_candidate=Mock(),
        warnings=warnings,
        mutated_workflow_uuid=child_uuid,
    )

    assert warnings == [
        {
            "code": "dependent_authoring_refresh_pending",
            "message": (
                f"实验操作已更新，但引用方工作流 {parent_uuid} 未能自动更新；"
                "请打开该工作流，检查组合节点参数和设备动作模板，重新编译并应用"
            ),
        }
    ]
