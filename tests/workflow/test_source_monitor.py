"""工作流源码监视器（Workflow Source Monitor）的公共命令接缝合同。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import source_publication
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'a' * 64}"


class RecordingSourceChangeService:
    """记录源码变化命令并可注入重试结果的测试适配器。"""

    def __init__(
        self,
        *,
        transient_failures: int = 0,
        pending_results: int = 0,
        enumeration_failures: int = 0,
        fatal_enumeration_failures: int = 0,
    ) -> None:
        """建立一个可控的源码变化命令接收端。

        参数：``transient_failures`` 是成功前抛出的瞬态故障次数；
        ``pending_results`` 是成功前返回未结算结果的次数；
        ``enumeration_failures`` 与 ``fatal_enumeration_failures`` 分别注入可恢复
        枚举故障和迫使工作线程退出的异常。
        返回：无；所有调用记录在实例内，供公开命令接缝断言。
        """

        self.registration = {"workflow_uuid": WORKFLOW_UUID}
        # ``signature`` 表示监视器当前观测到的源码文件世代，不是源码身份。
        self.signature: tuple[Any, ...] = ("file", 1)
        # ``attempts`` 保留每次提交使用的观测签名，用于证明重试没有丢失命令。
        self.attempts: list[tuple[Any, ...]] = []
        self._transient_failures = transient_failures
        self._pending_results = pending_results
        self._enumeration_failures = enumeration_failures
        self._fatal_enumeration_failures = fatal_enumeration_failures
        self.enumerated = threading.Event()
        self.submitted = threading.Event()

    def list_registered_sources(self) -> list[dict[str, str]]:
        """返回唯一已注册的工作流源码（Workflow Source）。

        参数：无。返回：含规范工作流 UUID 的来源注册列表。注入的瞬态枚举
        故障抛出 ``OSError``；致命枚举故障使当前工作线程退出一次。
        """

        self.enumerated.set()
        if self._fatal_enumeration_failures > 0:
            self._fatal_enumeration_failures -= 1
            raise SystemExit("注入的源码枚举线程退出")
        if self._enumeration_failures > 0:
            self._enumeration_failures -= 1
            raise OSError("注入的瞬态源码枚举故障")
        return [self.registration]

    def source_signature(
        self,
        workflow_uuid: str,
    ) -> tuple[Any, ...]:
        """返回调用时可见的源码文件签名。

        参数：``workflow_uuid`` 必须是本适配器当前授权的工作流身份。
        返回：当前文件签名；身份不匹配时测试直接失败。
        """

        assert workflow_uuid == self.registration["workflow_uuid"]
        return self.signature

    def submit_source_change(
        self,
        workflow_uuid: str,
        *,
        observed_signature: tuple[Any, ...],
    ) -> bool:
        """接收一项稳定源码变化命令并给出是否已结算。

        参数：``workflow_uuid`` 是规范工作流身份；``observed_signature`` 是
        去抖后稳定的文件世代。返回：``True`` 表示本命令已结算；``False`` 表示
        服务仍保留待恢复事实。瞬态故障通过 ``OSError`` 注入，监视器必须重试。
        """

        assert workflow_uuid == WORKFLOW_UUID
        self.attempts.append(observed_signature)
        if self._transient_failures > 0:
            self._transient_failures -= 1
            raise OSError("注入的瞬态源码读取故障")
        if self._pending_results > 0:
            self._pending_results -= 1
            return False
        self.submitted.set()
        return True


class PausingEnumerationService(RecordingSourceChangeService):
    """在指定枚举调用中建立可控阻塞点的测试服务。"""

    def __init__(self) -> None:
        """建立暂停请求、已暂停和恢复三个同步事件。

        参数：无。返回：无；默认正常枚举，测试显式请求后只暂停一次。
        """

        super().__init__()
        self.pause_next_enumeration = threading.Event()
        self.enumeration_paused = threading.Event()
        self.resume_enumeration = threading.Event()
        self._pause_consumed = False

    def list_registered_sources(self) -> list[dict[str, str]]:
        """按请求在返回活动来源前暂停一次工作线程。

        参数：无。返回：父类提供的唯一来源注册；暂停期间等待测试放行，等待超时
        抛出 ``TimeoutError`` 以避免测试永久挂起。
        """

        if self.pause_next_enumeration.is_set() and not self._pause_consumed:
            self._pause_consumed = True
            self.enumeration_paused.set()
            if not self.resume_enumeration.wait(timeout=3):
                raise TimeoutError("测试未放行源码枚举")
        return super().list_registered_sources()


class SourceOnlyCompiler:
    """把源码编译为不改变应用图的候选版本（Candidate Revision）。"""

    compiler_version = "f04-source-monitor-v1"
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
        """为源码同步测试生成确定性候选编译结果。

        参数：工作流 UUID、修订与来源 URI 提供编译身份；``python_source`` 是
        当前草稿；``applied_graph`` 是既有应用图。返回：只改变规范源码的候选。
        """

        # 这些身份由真实编译器用于诊断；本适配器只验证源码同步状态推进。
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


class NextGenerationSourceOnlyCompiler(SourceOnlyCompiler):
    """模拟子工作流应用后形成的新发布目录代际。"""

    compiler_version = "f04-source-monitor-v2"
    template_catalog_fingerprint = f"sha256:{'e' * 64}"


class InvalidCompositeCompiler(SourceOnlyCompiler):
    """模拟父组合工作流在子工作流尚未发布时的稳定失败。"""

    compiler_version = "f04-source-monitor-composite-missing"
    template_catalog_fingerprint = f"sha256:{'d' * 64}"

    def compile(self, **_arguments: Any) -> CandidateCompilation:
        return CandidateCompilation(
            diagnostics=[
                {
                    "severity": "error",
                    "code": "composite_child_not_found",
                    "message": "组合工作流创作合同校验失败",
                }
            ],
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """在有界时间内等待公开行为成立。

    参数：``predicate`` 是无副作用的公开行为观察；``timeout`` 是最长等待秒数。
    返回：无；超时会通过断言失败，避免测试无限等待。
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    assert predicate(), "等待源码监视公开行为超时"


def _service_with_source(
    tmp_path: Path,
) -> tuple[WorkflowService, WorkflowStore, Path]:
    """建立已注册且完成基线编译的真实工作流源码。

    参数：``tmp_path`` 是测试隔离目录。返回：工作流服务（WorkflowService）、
    持久存储和规范源码路径；调用者负责关闭存储。
    """

    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    service.create_workflow(
        name="源码监视合同",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="source_monitor_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    # ``source_path`` 是该来源身份唯一允许写入的规范文件路径。
    source_path = package_root / "workflows" / "demo.py"
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source="value = 'baseline'\n",
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    assert saved["candidate"] is not None
    return service, store, source_path


def test_monitor_waits_for_stable_signature_and_coalesces_burst() -> None:
    """连续文件变化必须合并为最后一个稳定源码变化命令。

    参数：无。返回：无；证明去抖不会提交中间文件世代。
    """

    service = RecordingSourceChangeService()
    monitor = WorkflowSourceMonitor(
        service,  # type: ignore[arg-type]
        interval_seconds=0.003,
        settle_seconds=0.03,
    )
    monitor.start()
    try:
        service.signature = ("file", 2)
        threading.Event().wait(0.012)
        service.signature = ("file", 3)
        threading.Event().wait(0.012)
        service.signature = ("file", 4)
        _wait_for(lambda: service.submitted.is_set())
        threading.Event().wait(0.05)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 4)]


def test_monitor_does_not_repeat_a_settled_signature() -> None:
    """已经结算的同一文件签名不得重复提交源码变化命令。

    参数：无。返回：无；证明普通轮询不会制造重复命令。
    """

    service = RecordingSourceChangeService()
    monitor = WorkflowSourceMonitor(
        service,  # type: ignore[arg-type]
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    try:
        assert service.submitted.wait(timeout=1)
        threading.Event().wait(0.05)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 1)]


def test_monitor_retries_a_transient_submission_failure() -> None:
    """瞬态提交失败必须保留同一源码变化命令并在退避后重试。

    参数：无。返回：无；证明瞬态基础设施故障不会被误记为已处理。
    """

    service = RecordingSourceChangeService(transient_failures=1)
    monitor = WorkflowSourceMonitor(
        service,  # type: ignore[arg-type]
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    try:
        assert service.submitted.wait(timeout=1)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 1), ("file", 1)]


def test_monitor_retries_when_service_keeps_recovery_pending() -> None:
    """服务尚未结算的源码恢复必须保留命令而不是确认文件世代。

    参数：无。返回：无；证明工作流服务（WorkflowService）拥有恢复状态解释权。
    """

    service = RecordingSourceChangeService(pending_results=1)
    monitor = WorkflowSourceMonitor(
        service,  # type: ignore[arg-type]
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    try:
        assert service.submitted.wait(timeout=1)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 1), ("file", 1)]


def test_monitor_survives_a_transient_source_enumeration_failure() -> None:
    """一次来源枚举故障不得杀死长期运行的源码监视线程。

    参数：无。返回：无；证明枚举失败后仍会提交同一活动来源的稳定变化命令。
    """

    service = RecordingSourceChangeService(enumeration_failures=1)
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    try:
        assert service.submitted.wait(timeout=1)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 1)]


def test_monitor_can_restart_after_stop_with_a_new_file_generation() -> None:
    """停止后的同一监视器实例必须能以新停止事件重新启动。

    参数：无。返回：无；证明第二次启动可以处理后续文件世代且不会复用已设置
    的停止事件。
    """

    service = RecordingSourceChangeService()
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    assert service.submitted.wait(timeout=1)
    monitor.stop()

    service.signature = ("file", 2)
    service.submitted.clear()
    monitor.start()
    try:
        assert service.submitted.wait(timeout=1)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 1), ("file", 2)]


def test_concurrent_monitor_start_calls_create_only_one_worker() -> None:
    """并发启动调用不得创建多个源码监视工作线程。

    参数：无。返回：无；通过唯一稳定源码变化命令证明生命周期锁完成单例启动。
    """

    service = RecordingSourceChangeService()
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    callers = [threading.Thread(target=monitor.start) for _index in range(12)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=1)
    try:
        assert service.submitted.wait(timeout=1)
        threading.Event().wait(0.05)
    finally:
        monitor.stop()

    assert all(not caller.is_alive() for caller in callers)
    assert service.attempts == [("file", 1)]


def test_monitor_can_restart_after_an_unexpected_worker_exit() -> None:
    """工作线程异常退出后不得留下阻止恢复启动的陈旧线程身份。

    参数：无。返回：无；第一次枚举主动退出，第二次启动仍能处理稳定源码命令。
    """

    service = RecordingSourceChangeService(fatal_enumeration_failures=1)
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    assert service.enumerated.wait(timeout=1)
    threading.Event().wait(0.05)
    monitor.start()
    try:
        assert service.submitted.wait(timeout=1)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 1)]


def test_start_during_stop_waits_then_creates_a_new_worker_generation() -> None:
    """停止正在 join 时并发启动必须等待并建立新的工作线程世代。

    参数：无。返回：无；通过受控枚举阻塞证明 ``start`` 不会把仍存活但已收到
    停止信号的旧线程误认为可复用运行线程。
    """

    service = PausingEnumerationService()
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    assert service.submitted.wait(timeout=1)
    service.signature = ("file", 2)
    service.submitted.clear()
    service.pause_next_enumeration.set()
    assert service.enumeration_paused.wait(timeout=1)
    stop_returned = threading.Event()
    restart_returned = threading.Event()

    def stop_monitor() -> None:
        """在线程中停止监视器并标记 join 已完成；参数无，返回无。"""

        monitor.stop()
        stop_returned.set()

    def restart_monitor() -> None:
        """在线程中请求新监视世代并标记启动调用返回；参数无，返回无。"""

        monitor.start()
        restart_returned.set()

    stopping_thread = threading.Thread(target=stop_monitor)
    stopping_thread.start()
    _wait_for(lambda: monitor._stop_event.is_set())
    restarting_thread = threading.Thread(target=restart_monitor)
    restarting_thread.start()
    try:
        assert not restart_returned.wait(timeout=0.05)
    finally:
        service.resume_enumeration.set()
    stopping_thread.join(timeout=1)
    restarting_thread.join(timeout=1)
    try:
        assert stop_returned.is_set()
        assert restart_returned.is_set()
        assert service.submitted.wait(timeout=1)
    finally:
        monitor.stop()

    assert service.attempts == [("file", 1), ("file", 2)]


def test_fatal_worker_failure_is_observable_and_restart_clears_health() -> None:
    """未分类致命异常必须形成稳定健康状态并可由显式重启恢复。

    参数：无。返回：无；第一次枚举退出后公开 ``fatal`` 与稳定错误码，新启动
    成功处理来源后公开 ``running``，停止后公开 ``stopped``。
    """

    service = RecordingSourceChangeService(fatal_enumeration_failures=1)
    monitor = WorkflowSourceMonitor(
        service,
        interval_seconds=0.003,
        settle_seconds=0.01,
    )
    monitor.start()
    _wait_for(lambda: monitor.health().state == "fatal")
    fatal_health = monitor.health()

    monitor.start()
    assert service.submitted.wait(timeout=1)
    running_health = monitor.health()
    monitor.stop()
    stopped_health = monitor.health()

    assert fatal_health.fatal_error_code == "unclassified_monitor_failure"
    assert running_health.state == "running"
    assert running_health.fatal_error_code is None
    assert stopped_health.state == "stopped"
    assert stopped_health.fatal_error_code is None


def test_service_rejects_a_stale_observed_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期文件世代不得触发对更新源码的越权编译。

    参数：``tmp_path`` 隔离真实源码和数据库。返回：无；证明签名变化时关闭失败
    （Fail-closed），只有重新稳定观测后的命令可推进候选。
    """

    monkeypatch.setattr(source_publication, "_PLATFORM", "linux")
    service, store, source_path = _service_with_source(tmp_path)
    # ``stale_signature`` 是修改前的文件世代，不能授权处理修改后的内容。
    stale_signature = service.source_signature(WORKFLOW_UUID)
    cursor = service.list_events(after_id=0)["items"][-1]["id"]
    source_path.write_text("value = 'new generation'\n", encoding="utf-8")
    current_signature = service.source_signature(WORKFLOW_UUID)
    try:
        assert not service.submit_source_change(
            WORKFLOW_UUID,
            observed_signature=stale_signature,
        )
        assert service.list_events(after_id=cursor)["items"] == []
        assert service.submit_source_change(
            WORKFLOW_UUID,
            observed_signature=current_signature,
        )
        events = service.list_events(after_id=cursor)["items"]
    finally:
        store.close()

    assert [event["data"]["cause"] for event in events] == [
        "external_draft_changed"
    ]


def test_same_hash_external_rewrite_does_not_emit_duplicate_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同内容外部重写不得重复生成候选版本或创作事件。

    参数：``tmp_path`` 隔离真实源码和数据库。返回：无；证明内容哈希而非文件
    修改时间决定工作流创作（Authoring）状态推进。
    """

    monkeypatch.setattr(source_publication, "_PLATFORM", "linux")
    service, store, source_path = _service_with_source(tmp_path)
    cursor = service.list_events(after_id=0)["items"][-1]["id"]
    original = source_path.read_bytes()
    source_path.write_bytes(original)
    # ``rewrite_signature`` 可能有新时间戳，但仍指向相同内容哈希。
    rewrite_signature = service.source_signature(WORKFLOW_UUID)
    try:
        assert service.submit_source_change(
            WORKFLOW_UUID,
            observed_signature=rewrite_signature,
        )
        events = service.list_events(after_id=cursor)["items"]
    finally:
        store.close()

    assert events == []


def test_catalog_generation_change_recompiles_unchanged_parent_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布目录变化必须让源码未变的组合父工作流重新校验。"""

    monkeypatch.setattr(source_publication, "_PLATFORM", "linux")
    service, store, _source_path = _service_with_source(tmp_path)
    old_signature = service.source_signature(WORKFLOW_UUID)
    cursor = service.list_events(after_id=0)["items"][-1]["id"]
    service.compiler = NextGenerationSourceOnlyCompiler()
    new_signature = service.source_signature(WORKFLOW_UUID)
    try:
        assert new_signature != old_signature
        assert service.submit_source_change(
            WORKFLOW_UUID,
            observed_signature=new_signature,
        )
        authoring = service.get_authoring(WORKFLOW_UUID)
        events = service.list_events(after_id=cursor)["items"]
    finally:
        store.close()

    assert authoring["candidate"]["template_catalog_fingerprint"] == (
        NextGenerationSourceOnlyCompiler.template_catalog_fingerprint
    )
    assert [event["data"]["cause"] for event in events] == ["catalog_changed"]


def test_startup_recovery_recompiles_persisted_composite_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启后当前发布目录必须取代旧的 composite_child_not_found 诊断。"""

    monkeypatch.setattr(source_publication, "_PLATFORM", "linux")
    service, store, source_path = _service_with_source(tmp_path)
    service.compiler = InvalidCompositeCompiler()
    source_path.write_text("value = 'parent composite'\n", encoding="utf-8")
    invalid_signature = service.source_signature(WORKFLOW_UUID)
    assert service.submit_source_change(
        WORKFLOW_UUID,
        observed_signature=invalid_signature,
    )
    assert service.get_authoring(WORKFLOW_UUID)["state"] == "draft_invalid"
    store.close()

    restarted_store = WorkflowStore(tmp_path / "workflow.db")
    restarted = WorkflowService(
        restarted_store,
        compiler=NextGenerationSourceOnlyCompiler(),
    )
    package_root = tmp_path / "package"
    restarted.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="source_monitor_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    try:
        restarted.recover_registered_sources()
        authoring = restarted.get_authoring(WORKFLOW_UUID)
    finally:
        restarted_store.close()

    assert authoring["state"] == "unapplied_source_only"
    assert authoring["draft"]["diagnostics"] == []
    assert authoring["candidate"]["template_catalog_fingerprint"] == (
        NextGenerationSourceOnlyCompiler.template_catalog_fingerprint
    )


def test_delete_rename_and_restore_stay_bound_to_canonical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除、重命名和恢复必须始终使用注册的规范来源身份。

    参数：``tmp_path`` 隔离真实源码和数据库。返回：无；证明旁路重命名文件
    不会成为新权威，恢复规范路径后产生同一工作流的恢复事件。
    """

    monkeypatch.setattr(source_publication, "_PLATFORM", "linux")
    service, store, source_path = _service_with_source(tmp_path)
    cursor = service.list_events(after_id=0)["items"][-1]["id"]
    renamed_path = source_path.with_name("renamed.py")
    source_path.rename(renamed_path)
    missing_signature = service.source_signature(WORKFLOW_UUID)
    try:
        assert service.submit_source_change(
            WORKFLOW_UUID,
            observed_signature=missing_signature,
        )
        assert service.get_authoring(WORKFLOW_UUID)["state"] == "draft_missing"
        assert renamed_path.exists()

        source_path.write_text("value = 'restored canonical'\n", encoding="utf-8")
        restored_signature = service.source_signature(WORKFLOW_UUID)
        assert service.submit_source_change(
            WORKFLOW_UUID,
            observed_signature=restored_signature,
        )
        authoring = service.get_authoring(WORKFLOW_UUID)
        events = service.list_events(after_id=cursor)["items"]
    finally:
        store.close()

    assert authoring["draft"]["python_source"] == "value = 'restored canonical'\n"
    assert renamed_path.read_text(encoding="utf-8") == "value = 'baseline'\n"
    assert [event["data"]["cause"] for event in events] == [
        "external_draft_changed",
        "recovered",
    ]
