"""工作流源码（Workflow Source）启动组合顺序的合同测试。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from unilabos.workflow import composition
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

# 两个 UUID 表示启动前已经由其他明确流程创建的工作流（Workflow）定义。
WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"
CATALOG_FINGERPRINT = f"sha256:{'f' * 64}"


class SourceOnlyCompiler:
    """把任意安全源码编译为只改源码的候选版本（Candidate Revision）。"""

    compiler_version = "f04-source-only-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """生成不改变已应用图的候选版本（Candidate Revision）。

        参数：工作流 UUID/修订和来源 URI 标识编译上下文；``python_source`` 是
        当前草稿；``applied_graph`` 是既有应用图。返回合法的源码型候选编译结果。
        """

        # 三个身份字段只参与真实编译诊断；本测试编译器刻意验证启动编排而非语法。
        del workflow_uuid, workflow_revision, source_uri
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


class BlockingCompiler(SourceOnlyCompiler):
    """在启动恢复（Startup Recovery）中提供可观察阻塞点的测试编译器。"""

    def __init__(
        self,
        recovery_entered: threading.Event,
        allow_recovery: threading.Event,
    ) -> None:
        """保存恢复进入和放行事件。

        参数：``recovery_entered`` 标记编译已开始；``allow_recovery`` 由主线程
        放行恢复。返回：无。
        """

        self._recovery_entered = recovery_entered
        self._allow_recovery = allow_recovery

    def compile(self, **kwargs: Any) -> CandidateCompilation:
        """阻塞一次启动编译，放行后返回源码型候选结果。

        参数：``kwargs`` 是工作流服务（WorkflowService）传入的完整编译上下文。
        返回：父类生成的候选编译结果；等待超时会抛出 ``TimeoutError``。
        """

        self._recovery_entered.set()
        if not self._allow_recovery.wait(timeout=3):
            raise TimeoutError("测试未放行启动恢复")
        return super().compile(**kwargs)


class RecordingMonitor:
    """记录源码监视器（Source Monitor）启动时可见的权威基线。"""

    observations: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, service: WorkflowService) -> None:
        """绑定待监视的工作流服务（WorkflowService）。

        参数：``service`` 是已经完成启动恢复的服务。返回：无。
        """

        self._service = service

    def start(self) -> None:
        """记录注册集合、草稿恢复结果与服务发布状态。

        参数：无。返回：无；记录只供组合根公开行为断言使用。
        """

        registrations = self._service.list_registered_sources()
        # ``workflow_uuids`` 是监视器首次启动时必须完整可见的注册身份集合。
        workflow_uuids = tuple(row["workflow_uuid"] for row in registrations)
        self.__class__.observations.append(
            {
                "workflow_uuids": workflow_uuids,
                "draft_sources": tuple(
                    self._service.get_authoring(workflow_uuid)["draft"][
                        "python_source"
                    ]
                    for workflow_uuid in workflow_uuids
                ),
                "published": composition.get_workflow_service() is self._service,
            }
        )

    def stop(self) -> None:
        """结束无后台线程的测试监视器。

        参数：无。返回：无；该适配器没有需要释放的线程资源。
        """


class PartialStartMonitor:
    """模拟启动失败且第一次停止也失败的监视器生命周期。"""

    instances: ClassVar[list[PartialStartMonitor]] = []

    def __init__(self, service: WorkflowService) -> None:
        """记录未完成发布的服务与后续停止次数。

        参数：``service`` 是启动恢复完成但尚未可靠发布的工作流服务。
        返回：无；实例加入测试观察集合。
        """

        self.service = service
        self.stop_calls = 0
        self.__class__.instances.append(self)

    def start(self) -> None:
        """注入监视器部分启动失败。

        参数：无。返回：无；始终抛出 ``RuntimeError`` 作为原始启动异常。
        """

        raise RuntimeError("注入的源码监视器启动失败")

    def stop(self) -> None:
        """第一次停止失败，第二次由重置流程成功接管清理。

        参数：无。返回：无；首次抛出 ``RuntimeError``，后续调用幂等成功。
        """

        self.stop_calls += 1
        if self.stop_calls == 1:
            raise RuntimeError("注入的源码监视器停止失败")


@pytest.fixture(autouse=True)
def clean_composition() -> Any:
    """为每个用例隔离进程级工作流组合根。

    参数：无。返回：pytest 生命周期控制值；前后都清理服务与监视器单例。
    """

    composition.reset_workflow_service_for_test()
    RecordingMonitor.observations.clear()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _write_package(
    selected_root: Path,
    *,
    package_id: str,
    workflow_uuid: str,
) -> None:
    """创建只有一项声明的可编辑包（Editable Package）。

    参数：``selected_root`` 是授权目录；``package_id`` 是包身份；
    ``workflow_uuid`` 是待绑定的既有工作流身份。返回：无。
    """

    source_path = selected_root / package_id / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("result = compile_workflow()\n", encoding="utf-8")
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        f"  name: {package_id}\n"
        "workflows:\n"
        f"  - workflow_uuid: {workflow_uuid}\n"
        f"    source: {package_id}/workflows/demo.py\n",
        encoding="utf-8",
    )


def _seed_workflows(working_dir: Path, *workflow_uuids: str) -> None:
    """在组合启动前创建明确的工作流（Workflow）定义。

    参数：``working_dir`` 决定 ``workflow_history.db``；``workflow_uuids`` 是
    要创建的稳定工作流身份。返回：无；数据库连接在返回前关闭。
    """

    store = WorkflowStore(working_dir / "workflow_history.db")
    service = WorkflowService(store)
    try:
        for workflow_uuid in workflow_uuids:
            service.create_workflow(
                workflow_uuid=workflow_uuid,
                name=f"workflow-{workflow_uuid[:8]}",
                tags=[],
                description=None,
                meta_data={},
            )
    finally:
        service.close()


def test_startup_registers_recovers_publishes_then_starts_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """监视启动时必须看到完整注册、已恢复草稿和已发布服务。

    参数：``tmp_path`` 隔离运行目录；``monkeypatch`` 注入测试监视适配器。
    返回：无；测试验证规范启动顺序的最终可观察截面。
    """

    working_dir = tmp_path / "unilabos-data"
    root_a = tmp_path / "editable-a"
    root_b = tmp_path / "editable-b"
    _write_package(root_a, package_id="alpha_lab", workflow_uuid=WORKFLOW_A_UUID)
    _write_package(root_b, package_id="beta_lab", workflow_uuid=WORKFLOW_B_UUID)
    _seed_workflows(working_dir, WORKFLOW_A_UUID, WORKFLOW_B_UUID)
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", RecordingMonitor)

    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=SourceOnlyCompiler(),
        editable_package_roots=(root_a, root_b),
    )

    assert composition.get_workflow_service() is service
    assert RecordingMonitor.observations == [
        {
            "workflow_uuids": (WORKFLOW_A_UUID, WORKFLOW_B_UUID),
            "draft_sources": (
                "result = compile_workflow()\n",
                "result = compile_workflow()\n",
            ),
            "published": True,
        }
    ]


def test_service_is_not_published_until_startup_recovery_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动恢复（Startup Recovery）完成前不得公开工作流服务。

    参数：``tmp_path`` 隔离运行目录；``monkeypatch`` 避免启动真实监视线程。
    返回：无；测试在线程阻塞点读取组合根公开服务。
    """

    working_dir = tmp_path / "unilabos-data"
    selected_root = tmp_path / "editable"
    _write_package(
        selected_root,
        package_id="alpha_lab",
        workflow_uuid=WORKFLOW_A_UUID,
    )
    _seed_workflows(working_dir, WORKFLOW_A_UUID)
    recovery_entered = threading.Event()
    allow_recovery = threading.Event()
    compose_finished = threading.Event()
    # ``outcome`` 跨线程保存最终服务或启动异常，避免后台失败被测试吞掉。
    outcome: dict[str, Any] = {}
    compiler = BlockingCompiler(recovery_entered, allow_recovery)
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", RecordingMonitor)

    def compose() -> None:
        """在后台线程启动工作流运行时并回传结果。

        参数：无。返回：无；任何异常都写入 ``outcome`` 供主线程断言。
        """

        try:
            outcome["service"] = composition.compose_workflow_runtime(
                working_dir,
                compiler=compiler,
                editable_package_roots=(selected_root,),
            )
        except BaseException as error:  # noqa: BLE001 - 跨线程保留原始启动失败
            outcome["error"] = error
        finally:
            compose_finished.set()

    thread = threading.Thread(target=compose, name="f04-startup-recovery")
    thread.start()
    try:
        assert recovery_entered.wait(timeout=1)
        visible_during_recovery = composition.get_workflow_service()
        allow_recovery.set()
        assert compose_finished.wait(timeout=3)
        thread.join(timeout=1)
    finally:
        allow_recovery.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert visible_during_recovery is None
    assert composition.get_workflow_service() is outcome["service"]


def test_startup_bootstraps_missing_definition_before_recovery_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动应在恢复与公开前原子创建清单声明的缺失工作流骨架。

    参数：``tmp_path`` 隔离运行目录；``monkeypatch`` 使用无线程监视适配器。
    返回：无；测试验证不需夹具预建定义，监视器首次观察即看到已恢复源码。
    """

    working_dir = tmp_path / "unilabos-data"
    selected_root = tmp_path / "editable"
    _write_package(
        selected_root,
        package_id="alpha_lab",
        workflow_uuid=WORKFLOW_A_UUID,
    )
    compiler = SourceOnlyCompiler()
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", RecordingMonitor)

    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        editable_package_roots=(selected_root,),
    )

    assert composition.get_workflow_service() is service
    assert service.get_workflow(WORKFLOW_A_UUID)["name"] == "alpha_lab.demo"
    assert RecordingMonitor.observations == [
        {
            "workflow_uuids": (WORKFLOW_A_UUID,),
            "draft_sources": ("result = compile_workflow()\n",),
            "published": True,
        }
    ]


def test_composition_identity_includes_compiler_and_authorized_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已发布组合不得在运行中切换编译器或授权包目录集合。

    参数：``tmp_path`` 隔离两个授权包；``monkeypatch`` 使用无线程监视适配器。
    返回：无；测试验证完全相同配置幂等，不同配置失败关闭。
    """

    working_dir = tmp_path / "unilabos-data"
    root_a = tmp_path / "editable-a"
    root_b = tmp_path / "editable-b"
    _write_package(root_a, package_id="alpha_lab", workflow_uuid=WORKFLOW_A_UUID)
    _write_package(root_b, package_id="beta_lab", workflow_uuid=WORKFLOW_B_UUID)
    _seed_workflows(working_dir, WORKFLOW_A_UUID, WORKFLOW_B_UUID)
    compiler = SourceOnlyCompiler()
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", RecordingMonitor)
    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        editable_package_roots=(root_a,),
    )

    assert (
        composition.compose_workflow_runtime(
            working_dir,
            compiler=compiler,
            editable_package_roots=(root_a,),
        )
        is service
    )
    with pytest.raises(RuntimeError):
        composition.compose_workflow_runtime(
            working_dir,
            compiler=SourceOnlyCompiler(),
            editable_package_roots=(root_a,),
        )
    with pytest.raises(RuntimeError):
        composition.compose_workflow_runtime(
            working_dir,
            compiler=compiler,
            editable_package_roots=(root_b,),
        )


def test_partial_monitor_start_failure_retains_retryable_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """监视器部分启动和首次停止失败时必须保留可重试清理所有权。

    参数：``tmp_path`` 隔离工作目录；``monkeypatch`` 注入失败监视器。
    返回：无；证明原始启动异常不被覆盖、服务不公开，重置可再次停止并释放服务。
    """

    working_dir = tmp_path / "runtime"
    PartialStartMonitor.instances.clear()
    original_monitor = composition.WorkflowSourceMonitor
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", PartialStartMonitor)

    with pytest.raises(RuntimeError) as caught:
        composition.compose_workflow_runtime(
            working_dir,
            editable_package_roots=(),
        )

    assert str(caught.value) == "注入的源码监视器启动失败"
    assert "注入的源码监视器停止失败" in "\n".join(caught.value.__notes__)
    assert composition.get_workflow_service() is None
    assert PartialStartMonitor.instances[0].stop_calls == 1

    composition.reset_workflow_service_for_test()
    assert PartialStartMonitor.instances[0].stop_calls == 2
    monkeypatch.setattr(composition, "WorkflowSourceMonitor", original_monitor)
    replacement = composition.compose_workflow_runtime(
        working_dir,
        editable_package_roots=(),
    )
    assert composition.get_workflow_service() is replacement
