"""可信工作流创作的草稿、候选和应用持久化合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .test_authoring_engine import WORKFLOW_UUID, _engine, _source


@pytest.fixture()
def authoring_service(tmp_path: Path) -> tuple[WorkflowService, Path]:
    """创建带真实 SQLite 与受控包目录的工作流服务（WorkflowService）。

    参数说明：``tmp_path`` 是 pytest 隔离目录；返回服务和规范作者源码路径，
    测试结束后关闭数据库连接。
    """

    package_root = tmp_path / "package"
    workflows_dir = package_root / "workflows"
    workflows_dir.mkdir(parents=True)
    source_path = workflows_dir / "sample.py"
    service = WorkflowService(
        WorkflowStore(tmp_path / "workflow.db"),
        compiler=_engine(),
    )
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Persisted name",
        tags=["keep"],
        description="Persisted description",
        meta_data={"owner": "keep"},
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="lab",
        package_root=package_root,
        relative_path="workflows/sample.py",
    )
    try:
        yield service, source_path
    finally:
        service.close()


def test_draft_candidate_apply_is_atomic_and_backend_shaped(
    authoring_service: tuple[WorkflowService, Path],
) -> None:
    """单一候选哈希应原子更新图、修订和规范源码。

    参数：``authoring_service`` 提供真实 SQLite 与源码工作区（Source
    Workspace）。返回：无；断言候选版本（Candidate）只凭哈希即可应用且不会
    留下候选。异常：任一图、修订或源码事实未共同提交时测试失败。
    """

    service, source_path = authoring_service
    draft = service.save_draft(
        WORKFLOW_UUID,
        python_source=_source(),
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )

    # ``candidate`` 是服务端持久并签发的候选版本（Candidate）。
    candidate = draft["candidate"]
    assert candidate is not None
    assert candidate["candidate_hash"].startswith("sha256:")
    assert candidate["template_catalog_fingerprint"] == (
        service.compiler.template_catalog_fingerprint
    )
    assert service.get_graph(WORKFLOW_UUID)["nodes"] == []

    applied = service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=candidate["candidate_hash"],
    )

    graph = service.get_graph(WORKFLOW_UUID)
    assert graph["workflow"]["revision"] == 2
    assert len(graph["nodes"]) == 2
    assert applied["apply_result"]["workflow_revision"] == 2
    authoring = applied["authoring"]
    assert (
        source_path.read_text(encoding="utf-8") == authoring["draft"]["python_source"]
    )
    assert authoring["candidate"] is None


def test_stale_candidate_fails_without_partial_graph_or_source_writeback(
    authoring_service: tuple[WorkflowService, Path],
) -> None:
    """候选哈希冲突必须保持已应用图和作者草稿不变。

    参数：``authoring_service`` 提供真实 SQLite 与源码工作区（Source
    Workspace）。返回：无；断言伪造候选哈希（Candidate Hash）稳定失败。
    异常：若冲突留下部分图或源码写回，测试失败。
    """

    service, source_path = authoring_service
    service.save_draft(
        WORKFLOW_UUID,
        python_source=_source(),
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    before_graph = service.get_graph(WORKFLOW_UUID)
    before_source = source_path.read_text(encoding="utf-8")

    with pytest.raises(WorkflowConflict) as failure:
        service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash="sha256:" + "0" * 64,
        )

    assert failure.value.code == "candidate_hash_conflict"
    assert service.get_graph(WORKFLOW_UUID) == before_graph
    assert source_path.read_text(encoding="utf-8") == before_source


def test_authoring_http_keeps_backend_response_envelope(
    authoring_service: tuple[WorkflowService, Path],
) -> None:
    """工作流创作 HTTP 接口必须保持后端（Backend）响应外层结构。"""

    service, _source_path = authoring_service
    client = TestClient(create_workflow_app(service))

    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": _source(),
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"code", "data"}
    assert payload["code"] == 0
    assert payload["data"]["candidate"]["candidate_hash"].startswith("sha256:")


def test_same_content_cas_compiles_external_draft_without_rewriting_source(
    authoring_service: tuple[WorkflowService, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IDE 对已落盘源码做同内容 CAS 时只签发候选，不得再次发布文件。

    参数：``authoring_service`` 提供真实来源文件和编译器；``monkeypatch`` 把
    物理写入替换为失败哨兵。返回：无；证明外部保存后的补编译保留作者文件
    世代，同时推进候选版本（Candidate）。异常：若服务仍重写同内容文件或未
    产生候选，测试失败。
    """

    service, source_path = authoring_service
    python_source = _source()
    source_path.write_text(python_source, encoding="utf-8")
    observed = service.get_authoring(WORKFLOW_UUID)
    assert observed["state"] == "applied_source_stale"

    def reject_rewrite(*_args: object, **_kwargs: object) -> None:
        """暴露同内容 CAS 中任何不必要的物理文件重写。"""

        raise AssertionError("same-content CAS must not rewrite source")

    monkeypatch.setattr(service, "_atomic_write", reject_rewrite)

    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=python_source,
        expected_draft_hash=observed["draft"]["draft_hash"],
        expected_workflow_revision=observed["workflow_revision"],
    )

    assert saved["candidate"] is not None
    assert saved["candidate"]["draft_hash"] == saved["draft"]["draft_hash"]
    assert source_path.read_text(encoding="utf-8") == python_source


@pytest.mark.parametrize("request_variant", ["legacy_three_tokens", "candidate_bundle"])
def test_authoring_apply_http_rejects_client_supplied_candidate_facts(
    authoring_service: tuple[WorkflowService, Path],
    request_variant: str,
) -> None:
    """应用接口只接受服务器签发的候选哈希并拒绝客户端事实组合。

    参数：``authoring_service`` 提供真实 SQLite 与源码工作区（Source Workspace）；
    ``request_variant`` 选择旧三令牌或额外候选包。返回：无；断言严格请求模型以
    Backend 业务码 1000 拒绝，且工作流修订（Workflow Revision）不推进。
    """

    service, _source_path = authoring_service
    draft = service.save_draft(
        WORKFLOW_UUID,
        python_source=_source(),
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    # ``candidate`` 是服务端持久并签发的候选版本（Candidate），客户端只能回传哈希。
    candidate = draft["candidate"]
    assert candidate is not None
    if request_variant == "legacy_three_tokens":
        body = {
            "expected_draft_hash": draft["draft"]["draft_hash"],
            "expected_workflow_revision": 1,
            "expected_candidate_hash": candidate["candidate_hash"],
        }
    else:
        body = {
            "candidate_hash": candidate["candidate_hash"],
            "candidate": candidate,
        }

    response = TestClient(create_workflow_app(service)).post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json=body,
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": 1000,
        "error": {"msg": "提交内容格式不正确"},
    }
    assert service.get_graph(WORKFLOW_UUID)["workflow"]["revision"] == 1
