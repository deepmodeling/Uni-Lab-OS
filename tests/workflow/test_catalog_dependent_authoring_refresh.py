"""目录依赖创作刷新深模块（Deep Module）的公开行为合同。"""

from __future__ import annotations

from unittest.mock import Mock, call

from unilabos.workflow.catalog_dependent_authoring_refresh import (
    refresh_catalog_dependent_authoring,
)


def test_refreshes_only_stale_dependents_and_isolates_failure() -> None:
    """只刷新具有可失效投影的依赖来源，并把单项异常转成提交后警告。

    参数：无。
    返回：无；断言刚应用的工作流（Workflow）与稳定来源被跳过，待刷新来源失败
    不向调用者抛出，且生成唯一可恢复警告。
    异常：筛选、回调顺序或故障隔离合同漂移时由 pytest 报告。
    """

    # 三个 UUID 分别代表刚变更来源、稳定来源和持有旧候选的依赖来源。
    mutated_workflow_uuid = "11111111-1111-4111-8111-111111111111"
    stable_workflow_uuid = "22222222-2222-4222-8222-222222222222"
    stale_workflow_uuid = "33333333-3333-4333-8333-333333333333"
    registrations = (
        {"workflow_uuid": mutated_workflow_uuid},
        {"workflow_uuid": stable_workflow_uuid},
        {"workflow_uuid": stale_workflow_uuid},
    )
    # 创作记录中的候选或诊断是软件包目录（Package Catalog）变化后需重建的投影。
    records = {
        stable_workflow_uuid: {"candidate": None, "diagnostics": []},
        stale_workflow_uuid: {"candidate": {"candidate_hash": "old"}, "diagnostics": []},
    }
    record_loader = Mock(side_effect=records.__getitem__)
    reconcile_callback = Mock(side_effect=RuntimeError("暂时无法重新编译"))
    # 警告集合属于已提交应用结果，刷新模块只能追加而不能回滚主应用。
    warnings: list[dict[str, str]] = []

    refresh_catalog_dependent_authoring(
        registrations=registrations,
        load_authoring_record=record_loader,
        reconcile_source=reconcile_callback,
        warnings=warnings,
        mutated_workflow_uuid=mutated_workflow_uuid,
    )

    assert record_loader.call_args_list == [
        call(stable_workflow_uuid),
        call(stale_workflow_uuid),
    ]
    reconcile_callback.assert_called_once_with(stale_workflow_uuid)
    assert warnings == [
        {
            "code": "dependent_authoring_refresh_pending",
            "message": "工作流已应用，但依赖创作草稿仍待重新编译",
        }
    ]
