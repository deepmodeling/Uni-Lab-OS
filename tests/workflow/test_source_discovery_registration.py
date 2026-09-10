"""工作流源码（Workflow Source）发现计划的原子注册合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.workflow.service import (
    WorkflowConflict,
    WorkflowError,
    WorkflowService,
)
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import WorkflowStore

# 三个 UUID 分别代表已存在、可被来源计划绑定的工作流（Workflow）身份。
WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"
WORKFLOW_C_UUID = "33333333-3333-4333-8333-333333333333"
# 该 UUID 刻意不预写数据库，用于证明显式安装可以原子创建工作流（Workflow）骨架。
MISSING_WORKFLOW_UUID = "44444444-4444-4444-8444-444444444444"


def _write_package(
    selected_root: Path,
    *,
    package_id: str,
    entries: tuple[tuple[str, str], ...],
) -> Path:
    """创建一个显式授权的可编辑包（Editable Package）。

    参数：``selected_root`` 是启动配置授权目录；``package_id`` 是稳定包身份；
    ``entries`` 逐项给出工作流 UUID 与包内源码路径。
    返回：实际 Python 包目录，供既有注册冲突用例复用同一物理身份。
    """

    package_root = selected_root / package_id
    package_root.mkdir(parents=True)
    manifest_lines = ["package:", f"  name: {package_id}", "workflows:"]
    for workflow_uuid, source_path in entries:
        # ``workflow_uuid`` 是待绑定工作流身份；``source_path`` 是 manifest 中的
        # 规范包相对工作流源码（Workflow Source）位置。
        manifest_lines.extend(
            (
                f"  - workflow_uuid: {workflow_uuid}",
                f"    source: {source_path}",
            )
        )
        source_file = selected_root / source_path
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text("result = compile_workflow()\n", encoding="utf-8")
    selected_root.joinpath("package.yaml").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="utf-8",
    )
    return package_root


@pytest.fixture()
def registration_service(tmp_path: Path) -> WorkflowService:
    """创建包含三个既有工作流（Workflow）的真实 SQLite 服务。

    参数：``tmp_path`` 是 pytest 隔离目录。
    返回：可执行来源批量注册的工作流服务（WorkflowService）；测试后关闭连接。
    """

    # ``store`` 是本用例唯一的本地工作流写模型（Workflow Write Model）。
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    for workflow_uuid in (WORKFLOW_A_UUID, WORKFLOW_B_UUID, WORKFLOW_C_UUID):
        service.create_workflow(
            workflow_uuid=workflow_uuid,
            name=f"workflow-{workflow_uuid[:8]}",
            tags=[],
            description=None,
            meta_data={},
        )
    try:
        yield service
    finally:
        service.close()


def test_complete_discovery_plan_registers_atomically(
    tmp_path: Path,
    registration_service: WorkflowService,
) -> None:
    """完整发现计划应一次注册全部工作流源码（Workflow Source）。

    参数：``tmp_path`` 保存授权包；``registration_service`` 提供既有工作流身份。
    返回：无；测试从工作流服务（WorkflowService）公开查询验证完整结果。
    """

    selected_root = tmp_path / "editable"
    _write_package(
        selected_root,
        package_id="alpha_lab",
        entries=(
            (WORKFLOW_A_UUID, "alpha_lab/workflows/a.py"),
            (WORKFLOW_B_UUID, "alpha_lab/workflows/b.py"),
        ),
    )
    # ``plan`` 是全量校验完成后才产生的不可变来源发现计划。
    plan = discover_editable_sources((selected_root,))

    registered = registration_service.replace_discovered_source_authorizations(plan)

    assert [row["workflow_uuid"] for row in registered] == [
        WORKFLOW_A_UUID,
        WORKFLOW_B_UUID,
    ]
    assert {
        row["source_uri"] for row in registration_service.list_registered_sources()
    } == {
        "package://alpha_lab/workflows/a.py",
        "package://alpha_lab/workflows/b.py",
    }


def test_repeating_exact_discovery_plan_is_idempotent(
    tmp_path: Path,
    registration_service: WorkflowService,
) -> None:
    """重复注册同一发现计划不得改写已持久化来源事实。

    参数：``tmp_path`` 保存授权包；``registration_service`` 是来源注册权威。
    返回：无；测试比较两次注册前后的完整公开记录。
    """

    selected_root = tmp_path / "editable"
    _write_package(
        selected_root,
        package_id="alpha_lab",
        entries=((WORKFLOW_A_UUID, "alpha_lab/workflows/a.py"),),
    )
    plan = discover_editable_sources((selected_root,))
    registration_service.replace_discovered_source_authorizations(plan)
    first_snapshot = registration_service.list_registered_sources()

    second_result = registration_service.replace_discovered_source_authorizations(plan)

    assert second_result == first_snapshot
    assert registration_service.list_registered_sources() == first_snapshot


def test_missing_workflow_is_bootstrapped_only_during_explicit_plan_install(
    tmp_path: Path,
    registration_service: WorkflowService,
) -> None:
    """缺失工作流（Workflow）只在显式安装完整发现计划时创建骨架。

    参数：``tmp_path`` 保存同时含既有和缺失身份的包；``registration_service``
    提供工作流服务（WorkflowService）。返回：无；测试证明只读发现不写库，而安装
    在同一事务中提交既有来源与缺失定义。
    """

    selected_root = tmp_path / "editable"
    _write_package(
        selected_root,
        package_id="alpha_lab",
        entries=(
            (WORKFLOW_A_UUID, "alpha_lab/workflows/a.py"),
            (MISSING_WORKFLOW_UUID, "alpha_lab/workflows/missing.py"),
        ),
    )
    plan = discover_editable_sources((selected_root,))
    before_install = registration_service.list_workflows(page_size=100)["total"]

    registered = registration_service.replace_discovered_source_authorizations(plan)

    assert before_install == 3
    assert [row["workflow_uuid"] for row in registered] == [
        WORKFLOW_A_UUID,
        MISSING_WORKFLOW_UUID,
    ]
    assert registration_service.list_workflows(page_size=100)["total"] == 4
    assert registration_service.get_workflow(MISSING_WORKFLOW_UUID)["name"] == (
        "alpha_lab.missing"
    )


@pytest.mark.parametrize(
    "collision",
    ("physical_path", "source_uri", "package_identity"),
)
def test_existing_identity_collision_preserves_registration_batch_exactly(
    tmp_path: Path,
    registration_service: WorkflowService,
    collision: str,
) -> None:
    """任一既有来源身份冲突都必须回滚整个新注册批次。

    参数：``tmp_path`` 隔离冲突目录；``registration_service`` 保存既有来源事实；
    ``collision`` 选择物理路径、来源 URI 或包身份冲突。
    返回：无；测试断言失败前后的公开注册记录逐字段相同。
    """

    if collision == "physical_path":
        selected_root = tmp_path / "editable"
        package_root = _write_package(
            selected_root,
            package_id="alpha_lab",
            entries=(
                (WORKFLOW_B_UUID, "alpha_lab/workflows/b.py"),
                (WORKFLOW_C_UUID, "alpha_lab/workflows/shared.py"),
            ),
        )
        registration_service.replace_active_editable_source_authorization(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id="legacy_alpha",
            package_root=package_root,
            relative_path="workflows/shared.py",
        )
        roots = (selected_root,)
    elif collision == "source_uri":
        existing_root = tmp_path / "existing-alpha"
        existing_root.mkdir()
        registration_service.replace_active_editable_source_authorization(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id="alpha_lab",
            package_root=existing_root,
            relative_path="workflows/shared.py",
        )
        selected_root = tmp_path / "editable"
        _write_package(
            selected_root,
            package_id="alpha_lab",
            entries=(
                (WORKFLOW_B_UUID, "alpha_lab/workflows/b.py"),
                (WORKFLOW_C_UUID, "alpha_lab/workflows/shared.py"),
            ),
        )
        roots = (selected_root,)
    else:
        existing_root = tmp_path / "existing-alpha"
        existing_root.mkdir()
        registration_service.replace_active_editable_source_authorization(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id="alpha_lab",
            package_root=existing_root,
            relative_path="workflows/existing.py",
        )
        beta_root = tmp_path / "editable-beta"
        alpha_root = tmp_path / "editable-alpha"
        _write_package(
            beta_root,
            package_id="beta_lab",
            entries=((WORKFLOW_B_UUID, "beta_lab/workflows/b.py"),),
        )
        _write_package(
            alpha_root,
            package_id="alpha_lab",
            entries=((WORKFLOW_C_UUID, "alpha_lab/workflows/c.py"),),
        )
        roots = (beta_root, alpha_root)

    # ``before`` 是冲突尝试前的权威来源事实，失败后必须逐字段保持不变。
    before = registration_service.list_registered_sources()
    plan = discover_editable_sources(roots)

    with pytest.raises(WorkflowConflict) as caught:
        registration_service.replace_discovered_source_authorizations(plan)

    assert caught.value.code == "invalid_input"
    assert registration_service.list_registered_sources() == before


def test_discovered_package_identity_change_fails_without_partial_registration(
    tmp_path: Path,
    registration_service: WorkflowService,
) -> None:
    """发现后的普通目录替换也不得绕过包目录稳定身份。

    参数：``tmp_path`` 保存原目录与替换目录；``registration_service`` 提供注册权威。
    返回：无；测试断言过期发现计划失败关闭且不提交任何来源。
    """

    selected_root = tmp_path / "editable"
    package_root = _write_package(
        selected_root,
        package_id="alpha_lab",
        entries=((WORKFLOW_A_UUID, "alpha_lab/workflows/a.py"),),
    )
    plan = discover_editable_sources((selected_root,))
    moved_root = selected_root / "moved-alpha"
    package_root.rename(moved_root)
    replacement_root = selected_root / "alpha_lab"
    replacement_root.joinpath("workflows").mkdir(parents=True)
    replacement_root.joinpath("workflows/a.py").write_text(
        "replacement = True\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkflowError) as caught:
        registration_service.replace_discovered_source_authorizations(plan)

    assert caught.value.code == "invalid_input"
    assert registration_service.list_registered_sources() == []
