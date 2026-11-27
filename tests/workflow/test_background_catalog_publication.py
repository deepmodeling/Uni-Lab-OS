"""固定点构建的局部目录不能替换其他请求读取的已提交完整目录。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.registry.test_f05_material_source_catalog import _Registry
from tests.workflow.test_c1_r2_static_expansion_contract import PARENT_WORKFLOW_UUID
from tests.workflow.test_c1_r4_production_wiring import PACKAGE_ID, _write_package
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.workflow_api import create_workflow_app
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.workspace_runtime.activation import workflow_source_plan_from_catalog
from unilabos.workflow import composition


@pytest.fixture
def composed_runtime(tmp_path):
    root = tmp_path / 'editable'
    root.mkdir()
    _write_package(root)
    (root / 'pyproject.toml').write_text(f'[project]\nname="{PACKAGE_ID}"\nversion="0.1.0"\n')
    (root / PACKAGE_ID / '__init__.py').write_text('')
    source = WorkspaceSource(root)
    plan = workflow_source_plan_from_catalog(source=source, catalog=compile_package_source(source))
    composition.reset_workflow_service_for_test()
    inventory = InventoryStore(str(tmp_path / 'inventory.db'))
    try:
        service, projection = composition.compose_local_workflow_template_runtime(
            tmp_path / 'runtime', inventory_store=inventory, registry=_Registry(),
            editable_source_discovery_plan=plan, start_source_monitor=False)
        yield service, projection
    finally:
        composition.reset_workflow_service_for_test()
        inventory.close()


@pytest.mark.parametrize('fail_build', [False, True])
@pytest.mark.parametrize('direct_refresh', [False, True])
def test_ready_parent_graph_keeps_complete_catalog_during_real_fixed_point_rebuild(
    composed_runtime, monkeypatch, fail_build, direct_refresh,
):
    service, projection = composed_runtime
    provider = service._definition_store._template_snapshot_provider
    committed = provider.snapshot()
    original_graph = service.get_graph(PARENT_WORKFLOW_UUID)
    assert original_graph['nodes']
    entered, release = threading.Event(), threading.Event()
    native_build = composition.build_published_workflow_generation
    observed_candidates = []

    def paused_build(**kwargs):
        if not observed_candidates:
            observed_candidates.append(provider.snapshot())
            entered.set()
            assert release.wait(3)
            if fail_build:
                raise composition.PublishedWorkflowGenerationError('模拟固定点构建失败')
        return native_build(**kwargs)

    monkeypatch.setattr(composition, 'build_published_workflow_generation', paused_build)
    with ThreadPoolExecutor(max_workers=1) as executor:
        rebuild = (lambda: projection.refresh(_Registry())) if direct_refresh else service._compiler_rebuilder
        future = executor.submit(rebuild)
        try:
            assert entered.wait(1)
            assert observed_candidates[0].fingerprint != committed.fingerprint
            assert provider.snapshot() is committed
            assert projection.snapshot().fingerprint == committed.fingerprint
            with TestClient(create_workflow_app(service)) as client:
                for _ in range(3):
                    response = client.get(f'/api/v1/workflows/{PARENT_WORKFLOW_UUID}/graph')
                    assert response.status_code == 200
                    assert response.json()['code'] == 0
            assert service.get_graph(PARENT_WORKFLOW_UUID) == original_graph
        finally:
            release.set()
        if fail_build:
            with pytest.raises(composition.RegistryTemplateProjectionError):
                future.result(timeout=3)
            assert provider.snapshot() is committed
        else:
            result = future.result(timeout=3)
            if not direct_refresh:
                assert provider.snapshot().fingerprint == result.template_catalog_fingerprint
            assert provider.snapshot().fingerprint == projection.snapshot().fingerprint
        assert service.get_graph(PARENT_WORKFLOW_UUID) == original_graph


def test_candidate_scopes_are_nested_thread_local_and_builders_are_serialized():
    provider = composition._MutableTemplateSnapshotProvider()
    committed, candidate, nested = object(), object(), object()
    provider.publish(committed)
    entered, attempted, second_entered, release = (threading.Event() for _ in range(4))

    def first_builder():
        with provider.candidate_scope():
            provider.replace(candidate)
            with provider.candidate_scope():
                provider.replace(nested)
                assert provider.snapshot() is nested
            assert provider.snapshot() is candidate
            entered.set()
            assert release.wait(3)

    def second_builder():
        attempted.set()
        with provider.candidate_scope():
            second_entered.set()
            assert provider.snapshot() is committed

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_builder)
        assert entered.wait(1)
        second = executor.submit(second_builder)
        try:
            assert attempted.wait(1)
            assert not second_entered.is_set()
            assert provider.snapshot() is committed
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    assert second_entered.is_set()
    assert provider.snapshot() is committed
