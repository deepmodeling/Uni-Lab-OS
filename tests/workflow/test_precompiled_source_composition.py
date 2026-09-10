"""预编译工作流源码（Workflow Source）组合接线测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.workflow.source_discovery import EditableSourceDiscoveryPlan


def test_precompiled_source_plan_skips_manifest_rediscovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预编译来源计划直接进入组合根，不再发现软件包清单。

    参数：``tmp_path`` 提供隔离工作流数据库目录；``monkeypatch``
    替换外部持久化与监视依赖。
    返回：无；断言来源登记使用调用者传入的同一不可变计划。
    异常：组合根重读 ``package.yaml`` 或改写计划时断言失败。
    """

    from unilabos.workflow import composition

    composition.reset_workflow_service_for_test()
    # ``source_plan`` 是包目录（PackageCatalog）编译阶段已
    # 完整验证的工作流源码（Workflow Source）登记计划。
    source_plan = EditableSourceDiscoveryPlan(registrations=(), root_identities=())
    observed_plans: list[EditableSourceDiscoveryPlan] = []

    class FakeStore:
        """隔离真实 SQLite 的工作流存储（WorkflowStore）替身。"""

        def __init__(self, _database_path: Path) -> None:
            """接受组合根的数据库路径。

            参数：``_database_path`` 是本测试不实际打开的 SQLite 路径。
            返回：无。异常：无。
            """

        def close(self) -> None:
            """关闭替身存储。

            参数：无。返回：无。异常：无。
            """

    class FakeService:
        """记录来源计划的工作流服务（WorkflowService）替身。"""

        def __init__(self, _store: FakeStore, **_kwargs: object) -> None:
            """接受组合根注入依赖。

            参数：``_store`` 是替身存储；``_kwargs`` 是本测试不使用的
            编译器、物料（Material）解析器与调度桥。
            返回：无。异常：无。
            """

        def replace_discovered_source_authorizations(
            self,
            plan: EditableSourceDiscoveryPlan,
        ) -> None:
            """记录原子替换的工作流源码计划。

            参数：``plan`` 是组合根应原样传递的预编译计划。
            返回：无。异常：无。
            """

            observed_plans.append(plan)

        def activate_registered_sources_to_fixed_point(self) -> None:
            """模拟恢复并应用预编译工作区源码。

            参数：无。返回：无。异常：无。
            """

        def close(self) -> None:
            """关闭替身服务。

            参数：无。返回：无。异常：无。
            """

    class FakeMonitor:
        """记录组合根拥有的最小工作流源码监视器（Source Monitor）生命周期。"""

        def start(self) -> None:
            """模拟启动工作流源码监视。

            参数：无。
            返回：无。
            异常：无。
            """

        def stop(self) -> None:
            """模拟停止工作流源码监视。

            参数：无。
            返回：无。
            异常：无。
            """

    monitor = FakeMonitor()

    def create_monitor(_service: object) -> FakeMonitor:
        """返回本用例唯一工作流源码监视器。

        参数：``_service`` 是组合根刚创建的工作流服务。
        返回：固定监视器替身。
        异常：无。
        """

        return monitor

    def reject_manifest_rediscovery(_roots: object) -> None:
        """拒绝预编译来源计划之后再次扫描软件包清单。

        参数：``_roots`` 是任何意外传入的源码根集合。
        返回：永不返回。
        异常：固定触发 pytest 失败，证明组合根违反单次冻结合同。
        """

        pytest.fail("预编译计划不得重读软件包清单")

    monkeypatch.setattr(composition, "WorkflowStore", FakeStore)
    monkeypatch.setattr(composition, "WorkflowService", FakeService)
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", create_monitor)
    monkeypatch.setattr(
        composition,
        "discover_editable_sources",
        reject_manifest_rediscovery,
    )
    try:
        service = composition.compose_workflow_runtime(
            tmp_path,
            editable_source_discovery_plan=source_plan,
        )
        assert isinstance(service, FakeService)
        assert observed_plans == [source_plan]
    finally:
        composition.reset_workflow_service_for_test()
