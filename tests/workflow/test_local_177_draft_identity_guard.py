"""LOCAL-177 跨工作流 Draft 导入的持久源码身份守护回归。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import WorkflowStore

S04_WORKFLOW_UUID = "1bc5a151-445a-5a53-b24a-7a4b521ac60c"
S06_WORKFLOW_UUID = "0b4e6fce-14bc-5866-a373-16ad25c7f8cf"


def _workflow_source(
    *,
    workflow_uuid: str,
    name: str,
    symbol: str,
    decorator_name: str = "workflow_definition",
) -> str:
    return f'''from unilabos.workflow.authoring import {decorator_name}, workflow_output


@{decorator_name}(
    workflow_uuid="{workflow_uuid}",
    displayname="{name}",
)
def {symbol}():
    return workflow_output()
'''


def _ambiguous_workflow_source() -> str:
    return f'''from unilabos.workflow.authoring import workflow, workflow_output


@workflow(
    workflow_uuid="{S04_WORKFLOW_UUID}",
    displayname="S04 主声明",
)
def s04_primary():
    return workflow_output()


@workflow(
    workflow_uuid="{S06_WORKFLOW_UUID}",
    displayname="S06 冲突声明",
)
def s06_secondary():
    return workflow_output()
'''


def _write_registered_s04_package(workspace: Path) -> tuple[Path, str]:
    package_root = workspace / "szlab_poly_studio"
    source_path = package_root / "workflows" / "s04_robot_stirring.py"
    source_path.parent.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "szlab-poly-studio"
version = "1.0.0"

[tool.setuptools.packages.find]
include = ["szlab_poly_studio*"]
""",
        encoding="utf-8",
    )
    (workspace / "package.yaml").write_text(
        f"""package:
  name: szlab_poly_studio

workflows:
  - workflow_uuid: {S04_WORKFLOW_UUID}
    source: szlab_poly_studio/workflows/s04_robot_stirring.py
""",
        encoding="utf-8",
    )
    source = _workflow_source(
        workflow_uuid=S04_WORKFLOW_UUID,
        name="S04 机械臂与磁搅联调",
        symbol="s04_robot_stirring",
    )
    source_path.write_text(source, encoding="utf-8")
    return source_path, source


@pytest.fixture()
def registered_s04(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Path, Path, dict[str, Any]]]:
    workspace = tmp_path / "editable"
    source_path, _source = _write_registered_s04_package(workspace)

    initial_plan = discover_editable_sources((workspace,))
    assert [
        registration.workflow_uuid for registration in initial_plan.registrations
    ] == [S04_WORKFLOW_UUID]

    store = WorkflowStore(tmp_path / "unilabos_data" / "workflow.db")
    service = WorkflowService(
        store,
        compiler=WorkflowAuthoringEngine(
            catalog=AuthoringCatalogSnapshot.from_entities([], []),
        ),
    )
    service.create_workflow(
        name="S04 机械臂与磁搅联调",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=S04_WORKFLOW_UUID,
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=S04_WORKFLOW_UUID,
        package_id="szlab_poly_studio",
        package_root=workspace / "szlab_poly_studio",
        relative_path="workflows/s04_robot_stirring.py",
    )
    aggregate = service.reconcile_registered_source(S04_WORKFLOW_UUID)

    with TestClient(create_workflow_app(service)) as client:
        yield client, workspace, source_path, aggregate
    store.close()


@pytest.mark.parametrize("decorator_name", ["workflow", "workflow_definition"])
def test_draft_put_rejects_s06_source_without_breaking_registered_s04(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
    decorator_name: str,
) -> None:
    """规范/兼容 S06 声明均不得覆盖 S04 已登记源码。

    参数：``registered_s04`` 提供真实 package、SQLite 和公共 HTTP 接缝；
    ``decorator_name`` 选择规范或兼容工作流装饰器。返回：无。
    """

    client, workspace, source_path, aggregate = registered_s04
    original_s04 = source_path.read_bytes()
    imported_s06 = _workflow_source(
        workflow_uuid=S06_WORKFLOW_UUID,
        name="S06 机械臂与加液联调",
        symbol="s06_robot_liquid_handling",
        decorator_name=decorator_name,
    )

    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": imported_s06,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    restarted_plan = discover_editable_sources((workspace,))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 3003
    assert payload["error"]["code"] == "workflow_identity_mismatch"
    assert S04_WORKFLOW_UUID in payload["error"]["msg"]
    assert S06_WORKFLOW_UUID in payload["error"]["msg"]
    assert source_path.read_bytes() == original_s04
    assert [
        registration.workflow_uuid for registration in restarted_plan.registrations
    ] == [S04_WORKFLOW_UUID]


def test_identity_guard_is_not_masked_by_an_earlier_authoring_diagnostic(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
) -> None:
    """模块级诊断不得遮蔽跨工作流身份并放行物理写入。"""

    client, _workspace, source_path, aggregate = registered_s04
    original_s04 = source_path.read_bytes()
    imported_s06 = '"""触发模块级诊断的说明文本。"""\n\n' + _workflow_source(
        workflow_uuid=S06_WORKFLOW_UUID,
        name="S06 机械臂与加液联调",
        symbol="s06_robot_liquid_handling",
        decorator_name="workflow",
    )

    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": imported_s06,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 3003
    assert source_path.read_bytes() == original_s04


def test_draft_put_still_preserves_an_incomplete_same_identity_draft(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
) -> None:
    """无法判定为其他工作流的不完整草稿仍应保存并返回诊断。"""

    client, _workspace, source_path, aggregate = registered_s04
    incomplete_source = "@workflow(\n"

    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": incomplete_source,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    assert saved["state"] == "draft_invalid"
    assert saved["draft"]["python_source"] == incomplete_source
    assert saved["draft"]["diagnostics"][0]["code"] == "syntax_error"
    assert source_path.read_text(encoding="utf-8") == incomplete_source


def test_draft_put_still_saves_complete_source_for_the_same_workflow_identity(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
) -> None:
    """同 UUID 的完整 Python 仍可以替换当前 Workflow Draft。"""

    client, workspace, source_path, aggregate = registered_s04
    updated_s04 = _workflow_source(
        workflow_uuid=S04_WORKFLOW_UUID,
        name="S04 机械臂与磁搅联调（已更新）",
        symbol="s04_robot_stirring",
    )

    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": updated_s04,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    assert saved["draft"]["python_source"] == updated_s04
    assert saved["draft"]["diagnostics"] == []
    assert saved["candidate"] is not None
    assert source_path.read_text(encoding="utf-8") == updated_s04
    restarted_plan = discover_editable_sources((workspace,))
    assert [
        registration.workflow_uuid for registration in restarted_plan.registrations
    ] == [S04_WORKFLOW_UUID]


def test_same_workflow_uuid_with_noncanonical_case_remains_savable(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
) -> None:
    """大小写不同但语义相同的 UUID 仍属于当前工作流。"""

    client, _workspace, source_path, aggregate = registered_s04
    updated_s04 = _workflow_source(
        workflow_uuid=S04_WORKFLOW_UUID.upper(),
        name="S04 机械臂与磁搅联调（UUID 大写）",
        symbol="s04_robot_stirring",
        decorator_name="workflow",
    )

    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": updated_s04,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    assert source_path.read_text(encoding="utf-8") == updated_s04


@pytest.mark.parametrize(
    "draft_source",
    [
        "def incomplete(",
        "def workflow_without_declaration():\n    pass\n",
        _workflow_source(
            workflow_uuid="not-a-uuid",
            name="无效 UUID",
            symbol="invalid_uuid_workflow",
            decorator_name="workflow",
        ),
        _workflow_source(
            workflow_uuid=S04_WORKFLOW_UUID,
            name="动态 UUID",
            symbol="dynamic_uuid_workflow",
            decorator_name="workflow",
        ).replace(
            f'workflow_uuid="{S04_WORKFLOW_UUID}"',
            "workflow_uuid=WORKFLOW_UUID",
        ),
        _ambiguous_workflow_source(),
    ],
    ids=["syntax", "missing", "invalid", "dynamic", "ambiguous"],
)
def test_non_unique_or_non_literal_identity_remains_a_savable_invalid_draft(
    registered_s04: tuple[TestClient, Path, Path, dict[str, Any]],
    draft_source: str,
) -> None:
    """无法唯一证明属于其他工作流的内容保持原草稿保存合同。"""

    client, _workspace, source_path, aggregate = registered_s04
    response = client.put(
        f"/api/v1/workflows/{S04_WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": draft_source,
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    assert source_path.read_text(encoding="utf-8") == draft_source
