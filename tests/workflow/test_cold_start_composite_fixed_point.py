"""组合工作流（Composite Workflow）冷启动依赖固定点合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.registry.test_f05_material_source_catalog import _Registry
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.workspace_runtime.activation import (
    workflow_source_plan_from_catalog,
)
from unilabos.workflow import composition
from unilabos.workflow.composition import compose_local_workflow_template_runtime
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowConflict, WorkflowError, WorkflowService
from unilabos.workflow.source_coordinates import source_ranges_fit
from unilabos.workflow.store import WorkflowStore

from .test_c1_r2_static_expansion_contract import (
    CHILD_WORKFLOW_UUID as PUBLISHED_CHILD_WORKFLOW_UUID,
)
from .test_c1_r2_static_expansion_contract import (
    PARENT_WORKFLOW_UUID as PUBLISHED_PARENT_WORKFLOW_UUID,
)
from .test_c1_r4_production_wiring import (
    PACKAGE_ID as PUBLISHED_PACKAGE_ID,
)
from .test_c1_r4_production_wiring import _write_package as _write_published_package

# 两个稳定身份刻意让父工作流（Workflow）按 UUID 排在子工作流之前。
PARENT_WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CHILD_WORKFLOW_UUID = "22222222-2222-4222-8222-222222222222"
INITIAL_CATALOG_FINGERPRINT = f"sha256:{'a' * 64}"
CHILD_READY_CATALOG_FINGERPRINT = f"sha256:{'b' * 64}"


class MutableCatalogCompiler:
    """提供源码不变、模板目录代际可变的可信编译接缝。"""

    compiler_version = "catalog-observation-v1"

    def __init__(self) -> None:
        """建立初始模板目录代际并清空编译记录。

        参数：无。
        返回：无；``catalog_fingerprint`` 可由测试推进，``compile_calls`` 记录
        每次编译绑定的工作流身份与目录指纹。
        异常：无。
        """

        # ``catalog_fingerprint`` 是监视器也必须观察的模板目录代际身份。
        self.catalog_fingerprint = INITIAL_CATALOG_FINGERPRINT
        # ``compile_calls`` 证明相同源码是否真正按新目录重新编译。
        self.compile_calls: list[tuple[str, str]] = []

    @property
    def template_catalog_fingerprint(self) -> str:
        """返回当前模板目录代际指纹。

        参数：无。
        返回：测试显式设置的规范 SHA-256 指纹。
        异常：无。
        """

        return self.catalog_fingerprint

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """用当前目录代际生成不改变应用图的可信候选。

        参数：``workflow_uuid``、``workflow_revision`` 和 ``source_uri`` 是编译
        上下文；``python_source`` 是当前工作流源码；``applied_graph`` 是应用图。
        返回：绑定当前目录指纹的源码候选。异常：无。
        """

        # 本适配器只验证源码与目录共同组成观测代，不解释修订和来源 URI。
        del workflow_revision, source_uri
        self.compile_calls.append((workflow_uuid, self.catalog_fingerprint))
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
            template_catalog_fingerprint=self.catalog_fingerprint,
        )


@pytest.fixture(autouse=True)
def _isolate_workflow_composition() -> Any:
    """隔离进程唯一工作流运行时（Workflow Runtime）。

    参数：无。
    返回：pytest 生命周期控制值。
    异常：组合根清理失败时原样传播，防止后续测试复用旧权威。
    """

    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _write_parent_before_child_package(package_root: Path) -> None:
    """写入父来源先于子来源的最小可编辑包（Editable Package）。

    参数：``package_root`` 是公开工作流组合根授权的包目录。
    返回：无；清单声明父、子两个稳定身份，源码内容只作为编译器输入证据。
    异常：文件系统写入失败时原样传播。
    """

    # ``source_root`` 是清单内两个工作流源码（Workflow Source）的规范父目录。
    source_root = package_root / "cold_start_lab" / "workflows"
    source_root.mkdir(parents=True)
    (source_root / "single_sample.py").write_text(
        "parent = material_transfer()\n",
        encoding="utf-8",
    )
    (source_root / "material_transfer.py").write_text(
        "child = transfer_material()\n",
        encoding="utf-8",
    )
    package_root.joinpath("package.yaml").write_text(
        "package:\n"
        "  name: cold_start_lab\n"
        "workflows:\n"
        f"  - workflow_uuid: {PARENT_WORKFLOW_UUID}\n"
        "    source: cold_start_lab/workflows/single_sample.py\n"
        f"  - workflow_uuid: {CHILD_WORKFLOW_UUID}\n"
        "    source: cold_start_lab/workflows/material_transfer.py\n",
        encoding="utf-8",
    )


def test_catalog_generation_change_recompiles_unchanged_workspace_source(
    tmp_path: Path,
) -> None:
    """模板目录换代必须让统一监视器重编译未改字节的工作流源码。

    参数：``tmp_path`` 隔离真实包与 SQLite。
    返回：无；断言源码文件签名不变时目录指纹仍改变公开观测签名，并通过
    ``submit_source_change`` 产生绑定新目录代际的候选版本（Candidate）。
    异常：若签名仍只含文件身份，本测试会在新旧签名相等处 RED。
    """

    # ``package_root`` 复用两个登记来源，但本断言只观察父工作流的稳定源码。
    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)
    # ``compiler`` 的目录代际可独立于工作流源码字节推进。
    compiler = MutableCatalogCompiler()
    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=compiler,
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )
    initial_signature = service.source_signature(PARENT_WORKFLOW_UUID)
    initial_compile_count = len(compiler.compile_calls)

    compiler.catalog_fingerprint = CHILD_READY_CATALOG_FINGERPRINT
    changed_signature = service.source_signature(PARENT_WORKFLOW_UUID)

    assert changed_signature != initial_signature
    assert service.submit_source_change(
        PARENT_WORKFLOW_UUID,
        observed_signature=changed_signature,
    )
    parent_authoring = service.get_authoring(PARENT_WORKFLOW_UUID)
    assert len(compiler.compile_calls) == initial_compile_count + 1
    assert parent_authoring["candidate"]["template_catalog_fingerprint"] == (
        CHILD_READY_CATALOG_FINGERPRINT
    )
    assert service.list_events(after_sequence=0)["items"][-1]["data"]["cause"] == (
        "catalog_changed"
    )


def test_workspace_activation_isolates_invalid_candidate_and_keeps_progressing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """单个候选应用失败必须成为该工作流诊断，不能阻断同包其他来源。

    参数：``tmp_path`` 隔离真实来源和 SQLite；``monkeypatch`` 只让父工作流应用
    稳定失败；``capsys`` 验证启动日志携带可定位身份。返回：无；子工作流仍被
    应用，父候选被撤销并保存原错误码，日志包含父 UUID。异常：若固定点把单项
    业务错误传播到组合根，测试保持 RED。
    """

    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)
    compiler = MutableCatalogCompiler()
    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=compiler,
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )
    original_apply = service.apply_authoring

    def fail_parent_candidate(
        workflow_uuid: str,
        *,
        candidate_hash: str,
        preserve_author_source: bool = False,
    ) -> dict[str, Any]:
        if workflow_uuid == PARENT_WORKFLOW_UUID:
            raise WorkflowError("candidate_invalid")
        return original_apply(
            workflow_uuid,
            candidate_hash=candidate_hash,
            preserve_author_source=preserve_author_source,
        )

    monkeypatch.setattr(service, "apply_authoring", fail_parent_candidate)

    service.activate_registered_sources_to_fixed_point()

    parent = service.get_authoring(PARENT_WORKFLOW_UUID)
    child = service.get_authoring(CHILD_WORKFLOW_UUID)
    assert parent["candidate"] is None
    assert {item["code"] for item in parent["draft"]["diagnostics"]} == {
        "candidate_invalid"
    }
    assert child["state"] == "applied"
    assert PARENT_WORKFLOW_UUID in capsys.readouterr().err


def test_workspace_recovery_propagates_catalog_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动恢复不得把目录基础设施失败吞成工作区 ready。

    参数：``tmp_path`` 隔离来源与 SQLite；``monkeypatch`` 注入目录不可用错误。
    返回：无；断言错误传播到组合根。异常：若恢复继续捕获 ``RuntimeError``，
    本测试会因没有抛错而保持 RED。
    """

    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)
    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=MutableCatalogCompiler(),
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )

    def fail_reconcile(
        workflow_uuid: str,
        *,
        force_compile: bool = False,
        preserve_author_source: bool = False,
    ) -> dict[str, Any]:
        del workflow_uuid, force_compile, preserve_author_source
        raise WorkflowError("template_catalog_unavailable")

    monkeypatch.setattr(service, "reconcile_registered_source", fail_reconcile)

    with pytest.raises(WorkflowError) as captured:
        service.recover_registered_sources(preserve_author_source=True)
    assert captured.value.code == "template_catalog_unavailable"


def test_workspace_activation_propagates_authority_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定点自动应用不得把目录/CAS 权威冲突降级为普通草稿诊断。

    参数：``tmp_path`` 隔离来源与 SQLite；``monkeypatch`` 在应用点注入目录代际
    冲突。返回：无；断言冲突传播，阻止组合根发布 ready。异常：若自动激活仍
    捕获全部 ``WorkflowError``，本测试保持 RED。
    """

    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)
    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=MutableCatalogCompiler(),
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )

    def fail_apply(
        workflow_uuid: str,
        *,
        candidate_hash: str,
        preserve_author_source: bool = False,
    ) -> dict[str, Any]:
        del workflow_uuid, candidate_hash, preserve_author_source
        raise WorkflowConflict("template_catalog_conflict")

    monkeypatch.setattr(service, "apply_authoring", fail_apply)

    with pytest.raises(WorkflowConflict) as captured:
        service.activate_registered_sources_to_fixed_point()
    assert captured.value.code == "template_catalog_conflict"


def test_workspace_activation_fails_closed_after_catalog_rebuild_warning(
    tmp_path: Path,
) -> None:
    """自动应用提交后目录重建失败不得让组合根发布 ready。

    参数：``tmp_path`` 隔离来源与 SQLite。返回：无；真实 Apply 已提交图后，
    目录重建器失败产生的 warning 必须在固定点边界重新升级为目录不可用错误。
    异常：若自动激活忽略提交后 warning，本测试因没有抛错而保持 RED。
    """

    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)

    def fail_catalog_rebuild() -> MutableCatalogCompiler:
        raise RuntimeError("目录重建基础设施不可用")

    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=MutableCatalogCompiler(),
        compiler_rebuilder=fail_catalog_rebuild,
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )

    with pytest.raises(WorkflowError) as captured:
        service.activate_registered_sources_to_fixed_point()
    assert captured.value.code == "template_catalog_unavailable"


def test_workspace_activation_fails_closed_after_dependent_refresh_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动应用提交后的父/子依赖重编译失败不得被 warning 隔离成 ready。

    参数：``tmp_path`` 隔离来源与 SQLite；``monkeypatch`` 让首次恢复保持原状，
    再只在已提交 Apply 的依赖刷新阶段注入失败。返回：无；固定点把真实刷新
    warning 升级为目录不可用。异常：若 Apply 结果 warning 未被检查则保持 RED。
    """

    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)
    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=MutableCatalogCompiler(),
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )
    original_reconcile = service.reconcile_registered_source

    def skip_recovery(*, preserve_author_source: bool = False) -> None:
        del preserve_author_source

    def fail_dependent_refresh(
        workflow_uuid: str,
        *,
        force_compile: bool = False,
        preserve_author_source: bool = False,
    ) -> dict[str, Any]:
        if workflow_uuid == CHILD_WORKFLOW_UUID and force_compile:
            raise RuntimeError("依赖来源无法按新目录重编译")
        return original_reconcile(
            workflow_uuid,
            force_compile=force_compile,
            preserve_author_source=preserve_author_source,
        )

    monkeypatch.setattr(service, "recover_registered_sources", skip_recovery)
    monkeypatch.setattr(
        service,
        "reconcile_registered_source",
        fail_dependent_refresh,
    )

    with pytest.raises(WorkflowError) as captured:
        service.activate_registered_sources_to_fixed_point()
    assert captured.value.code == "template_catalog_unavailable"


def test_product_composition_does_not_publish_after_catalog_rebuild_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """产品组合根在自动应用后的目录重建失败时不得发布工作流 ready。

    参数：``tmp_path`` 隔离真实预编译工作区与两份 SQLite；``monkeypatch`` 让
    产品模板投影第一次建立成功、首次 Apply 后刷新失败。返回：无；组合入口抛
    稳定目录错误，且工作流服务和模板投影单例都保持未发布。异常：若产品入口
    绕过固定点 fail-closed，本测试会观察到已发布权威并保持 RED。
    """

    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_published_package(selected_root)
    selected_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "c1-product-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    selected_root.joinpath(PUBLISHED_PACKAGE_ID, "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    workspace_source = WorkspaceSource(selected_root)
    source_plan = workflow_source_plan_from_catalog(
        source=workspace_source,
        catalog=compile_package_source(workspace_source),
    )
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    original_refresh = composition.RegistryTemplateProjection.refresh
    refresh_count = 0

    def fail_second_refresh(
        projection: Any,
        registry_snapshot: Any,
    ) -> Any:
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 2:
            raise RuntimeError("自动应用后目录刷新失败")
        return original_refresh(projection, registry_snapshot)

    monkeypatch.setattr(
        composition.RegistryTemplateProjection,
        "refresh",
        fail_second_refresh,
    )
    try:
        with pytest.raises(WorkflowError) as captured:
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=_Registry(),
                editable_source_discovery_plan=source_plan,
                start_source_monitor=False,
            )
        assert captured.value.code == "template_catalog_unavailable"
        assert composition.get_workflow_service() is None
        assert composition.get_registry_template_projection() is None
    finally:
        composition.reset_workflow_service_for_test()
        inventory_store.close()


def test_workspace_activation_refreshes_parent_after_child_application(
    tmp_path: Path,
) -> None:
    """工作区自动应用子工作流后必须重编译并应用父工作流且保留源码。

    参数：``tmp_path`` 隔离全新工作流、库存 SQLite 与真实作者源码。
    返回：无；通过真实 ``apply_authoring``、编译器重建器和模板投影自动发布
    子、父合同，并保持作者源码字节不变。异常：若固定点激活仍需手工 PUT
    父源码，或把扫描误当成作者编辑而规范化源码，测试保持 RED。
    """

    # ``selected_root`` 是生产组合根明确授权的可编辑包（Editable Package）。
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_published_package(selected_root)
    child_source_path = (
        selected_root / PUBLISHED_PACKAGE_ID / "workflows" / "child.py"
    )
    parent_source_path = (
        selected_root / PUBLISHED_PACKAGE_ID / "workflows" / "parent.py"
    )
    parent_source_path.write_text(
        parent_source_path.read_text(encoding="utf-8").replace(
            "    # unilab:node_uuid=",
            "    # [配样]: 保留作者的中文节点说明\n"
            "    # unilab:node_uuid=",
        ),
        encoding="utf-8",
    )
    source_bytes_before_activation = {
        child_source_path: child_source_path.read_bytes(),
        parent_source_path: parent_source_path.read_bytes(),
    }
    # ``pyproject.toml`` 与包初始化文件让测试走产品工作区统一包目录编译路径，
    # 避免遗留可编辑根缺少模块/符号目录身份而退化为未登记语义。
    selected_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "c1-product-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    selected_root.joinpath(PUBLISHED_PACKAGE_ID, "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    workspace_source = WorkspaceSource(selected_root)
    package_catalog = compile_package_source(workspace_source)
    source_plan = workflow_source_plan_from_catalog(
        source=workspace_source,
        catalog=package_catalog,
    )
    # ``inventory_store`` 为模板投影提供本地资源模板身份持久化边界。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_source_discovery_plan=source_plan,
            start_source_monitor=False,
        )
        child_authoring = service.get_authoring(PUBLISHED_CHILD_WORKFLOW_UUID)
        refreshed_parent = service.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)
        assert child_authoring["state"] == "applied"
        assert refreshed_parent["state"] == "applied"
        assert refreshed_parent["candidate"] is None
        assert refreshed_parent["draft"]["diagnostics"] == []
        applied_source = refreshed_parent["applied_source"]
        assert applied_source is not None
        assert applied_source["python_source"] == parent_source_path.read_text(
            encoding="utf-8"
        )
        assert applied_source["source_map"]
        assert source_ranges_fit(
            applied_source["python_source"],
            applied_source["source_map"],
        )
        assert {
            path: path.read_bytes() for path in source_bytes_before_activation
        } == source_bytes_before_activation
    finally:
        composition.reset_workflow_service_for_test()
        inventory_store.close()


def test_workspace_activation_applies_composites_child_first_to_fixed_point(
    tmp_path: Path,
) -> None:
    """预编译工作区激活必须在发布 ready 前自动应用子工作流和组合父工作流。

    参数：``tmp_path`` 隔离真实工作区、工作流与库存 SQLite。
    返回：无；父 UUID 刻意排在子 UUID 前，最终二者仍都处于 ``applied``，
    证明启动扫描不是按登记顺序碰运气，而是推进到了组合依赖固定点。
    异常：若仍只生成候选或要求人工先应用子工作流，本测试保持 RED。
    """

    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_published_package(selected_root)
    selected_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "c1-product-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    selected_root.joinpath(PUBLISHED_PACKAGE_ID, "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    workspace_source = WorkspaceSource(selected_root)
    package_catalog = compile_package_source(workspace_source)
    source_plan = workflow_source_plan_from_catalog(
        source=workspace_source,
        catalog=package_catalog,
    )
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_source_discovery_plan=source_plan,
            start_source_monitor=False,
        )

        child = service.get_authoring(PUBLISHED_CHILD_WORKFLOW_UUID)
        parent = service.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)

        assert child["state"] == "applied"
        assert child["candidate"] is None
        assert parent["state"] == "applied"
        assert parent["candidate"] is None
        assert parent["draft"]["diagnostics"] == []
        applied_workflow_uuids = [
            item["data"]["workflow_uuid"]
            for item in service.list_events(after_sequence=0)["items"]
            if item["data"].get("cause") == "applied"
        ]
        assert applied_workflow_uuids.index(PUBLISHED_CHILD_WORKFLOW_UUID) < (
            applied_workflow_uuids.index(PUBLISHED_PARENT_WORKFLOW_UUID)
        )
    finally:
        composition.reset_workflow_service_for_test()
        inventory_store.close()


def test_restart_keeps_workspace_composites_applied_against_current_catalog(
    tmp_path: Path,
) -> None:
    """重启恢复必须保持预编译工作区的子、父工作流完整应用。

    参数：``tmp_path`` 隔离真实工作区与 SQLite。
    返回：无；第一进程把子、父工作流应用到固定点，第二进程继续公开无候选、
    无诊断的应用状态。异常：若恢复重新引入组合依赖诊断则测试失败。
    """

    # ``selected_root`` 包含真实父子作者源码和统一包目录声明。
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_published_package(selected_root)
    selected_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "c1-product-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    selected_root.joinpath(PUBLISHED_PACKAGE_ID, "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    workspace_source = WorkspaceSource(selected_root)
    package_catalog = compile_package_source(workspace_source)
    source_plan = workflow_source_plan_from_catalog(
        source=workspace_source,
        catalog=package_catalog,
    )
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        first_service, _first_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_source_discovery_plan=source_plan,
            start_source_monitor=False,
        )
        first_child = first_service.get_authoring(PUBLISHED_CHILD_WORKFLOW_UUID)
        first_parent = first_service.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)
        assert first_child["state"] == "applied"
        assert first_parent["state"] == "applied"
        composition.reset_workflow_service_for_test()

        restarted, _restarted_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_source_discovery_plan=source_plan,
            start_source_monitor=False,
        )
        restarted_parent = restarted.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)

        assert restarted_parent["state"] == "applied"
        assert restarted_parent["candidate"] is None
        assert restarted_parent["draft"]["diagnostics"] == []
    finally:
        composition.reset_workflow_service_for_test()
        inventory_store.close()


def test_missing_source_recovery_does_not_require_template_catalog(
    tmp_path: Path,
) -> None:
    """缺失源码恢复不得要求尚未装配的模板目录（Template Catalog）。

    参数：``tmp_path`` 隔离工作流 SQLite 与空包目录。
    返回：无；无编译器服务强制恢复缺失工作流源码（Workflow Source）后公开
    ``draft_missing``，且不会伪造目录代际。异常：若恢复尾部仍调用
    ``_catalog_fingerprint``，测试以 ``template_catalog_unavailable`` 失败。
    """

    # ``store`` 与 ``service`` 模拟仅承担来源授权、尚未装配创作编译器的启动阶段。
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    package_root = tmp_path / "missing_source_lab"
    package_root.mkdir()
    service.create_workflow(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        name="缺失源码恢复",
        tags=[],
        description=None,
        meta_data={},
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        package_id="missing_source_lab",
        package_root=package_root,
        relative_path="workflows/missing.py",
    )
    try:
        recovered = service.reconcile_registered_source(
            PARENT_WORKFLOW_UUID,
            force_compile=True,
        )
    finally:
        service.close()

    assert recovered["state"] == "draft_missing"
    assert recovered["candidate"] is None
