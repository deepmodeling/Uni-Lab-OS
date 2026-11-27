"""实验操作应用后自动刷新引用方工作流的公开行为合同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.registry.test_f05_material_source_catalog import _Registry
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.service import WorkflowService

from .test_c1_r2_static_expansion_contract import (
    CHILD_WORKFLOW_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
)
from .test_c1_r4_production_wiring import _child_source, _write_package
from .test_published_composite_restart import (
    _child_source as _executable_child_source,
)
from .test_published_composite_restart import (
    _RegistryWithPump,
)


def _apply_source(
    service: WorkflowService,
    workflow_uuid: str,
    python_source: str,
) -> dict[str, object]:
    """保存并应用一份领域包工作流源码。

    参数：``service`` 是真实 Local 工作流服务，``workflow_uuid`` 是待修改定义，
    ``python_source`` 是完整作者源码。返回：公共 Apply 结果。异常：草稿、目录或
    候选冲突由公共服务原样抛出；测试不绕过源码 CAS 和候选复核。
    """

    current = service.get_authoring(workflow_uuid)
    draft = current["draft"]
    assert isinstance(draft, dict)
    saved = service.save_draft(
        workflow_uuid,
        python_source=python_source,
        expected_draft_hash=str(draft["draft_hash"]),
        expected_workflow_revision=int(current["workflow_revision"]),
    )
    candidate = saved["candidate"]
    assert isinstance(candidate, dict)
    return service.apply_authoring(
        workflow_uuid,
        candidate_hash=str(candidate["candidate_hash"]),
    )


def test_child_apply_does_not_refresh_parent_before_publish(
    tmp_path: Path,
) -> None:
    """子流程应用但尚未发布时，父工作流仍保持旧合同。

    参数：``tmp_path`` 隔离真实领域包、工作流定义目录和库存库。返回：无；断言
    子流程只保存/应用时父修订、调用节点和已发布合同 pin 不变。发布后的刷新由
    ``publish_workflow_contract`` 路径负责，并由独立发布回归覆盖。异常：组合根、
    源码协调或断言失败时由 pytest 报告。
    """

    reset_workflow_service_for_test()
    package_root = tmp_path / "editable"
    package_root.mkdir()
    _write_package(package_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_package_roots=(package_root,),
        )
        parent_before = service.get_graph(PARENT_WORKFLOW_UUID)
        child_before = service.get_graph(CHILD_WORKFLOW_UUID)
        child_revision = int(child_before["workflow"]["revision"])
        task_before = service.create_workflow_task(
            workflow_uuid=PARENT_WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )

        result = _apply_source(
            service,
            CHILD_WORKFLOW_UUID,
            _child_source().replace("Published child", "Published child v2"),
        )

        assert result["apply_result"]["warnings"] == []
        child_after = service.get_graph(CHILD_WORKFLOW_UUID)
        assert int(child_after["workflow"]["revision"]) == child_revision + 1
        parent_after_apply = service.get_graph(PARENT_WORKFLOW_UUID)
        # ``node_templates`` 是当前目录投影，子源码应用后可以反映新模板描述；
        # 父工作流自身的持久定义、调用节点和连线不能在发布前改变。
        assert parent_after_apply["workflow"] == parent_before["workflow"]
        assert parent_after_apply["nodes"] == parent_before["nodes"]
        assert parent_after_apply["edges"] == parent_before["edges"]
        assert service.get_authoring(PARENT_WORKFLOW_UUID)["state"] == "applied"
        frozen_before = service.get_workflow_task(task_before["uuid"])[
            "workflow_snapshot"
        ]
        frozen_invocation = next(
            node for node in frozen_before["nodes"] if node["uuid"] == INVOCATION_UUID
        )
        assert (
            frozen_invocation["meta_data"]["unilab"]["composite"][
                "child_workflow_revision"
            ]
            == child_revision
        )

        task_after = service.create_workflow_task(
            workflow_uuid=PARENT_WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        new_invocation = next(
            node
            for node in task_after["workflow_snapshot"]["nodes"]
            if node["uuid"] == INVOCATION_UUID
        )
        assert (
            new_invocation["meta_data"]["unilab"]["composite"][
                "child_workflow_revision"
            ]
            == child_revision
        )
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_first_publication_recompiles_parent_blocked_by_unpublished_child(
    tmp_path: Path,
) -> None:
    """首次发布实验操作后须恢复此前只因未发布而失败的父工作流。

    参数：``tmp_path`` 隔离真实领域包、发布目录和库存库。返回：无；先确认父
    工作流因找不到未发布子操作而关闭，再经公共发布接口确认父源码自动重编译
    并应用。异常：若发布只更新子目录而遗漏已登记父源码，状态断言保持 RED。
    """

    reset_workflow_service_for_test()
    package_root = tmp_path / "editable"
    package_root.mkdir()
    _write_package(package_root)
    # 显式存在的空发布目录代表一个已经启用发布制度的领域包；与“从未有过
    # 发布目录”的旧包兼容路径区分开，确保本测试验证新包的发布门槛。
    # 发布目录属于 manifest 声明的实际 Python 包目录，而不是选择目录；否则
    # 组合根无法看到它，测试会误走“从未有过发布目录”的兼容路径。
    package_root.joinpath("c1_product_lab", "workflow_publications.json").write_text(
        '{"version": 1, "publications": []}\n',
        encoding="utf-8",
    )
    package_root.joinpath("c1_product_lab/workflows/child.py").write_text(
        _executable_child_source(CHILD_WORKFLOW_UUID).replace(
            "def child():",
            "def prepare_sample():",
        ),
        encoding="utf-8",
    )
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_RegistryWithPump(),
            editable_package_roots=(package_root,),
        )
        parent_before = service.get_authoring(PARENT_WORKFLOW_UUID)
        assert parent_before["state"] == "draft_invalid"
        assert {item["code"] for item in parent_before["draft"]["diagnostics"]} == {
            "composite_child_not_found"
        }

        child_revision = int(
            service.get_graph(CHILD_WORKFLOW_UUID)["workflow"]["revision"]
        )
        published = service.publish_workflow_contract(
            CHILD_WORKFLOW_UUID,
            revision=child_revision,
        )

        assert published["workflow_uuid"] == CHILD_WORKFLOW_UUID
        parent_after = service.get_authoring(PARENT_WORKFLOW_UUID)
        assert parent_after["state"] == "applied"
        assert parent_after["draft"]["diagnostics"] == []
        assert {node["type"] for node in service.get_graph(PARENT_WORKFLOW_UUID)["nodes"]} >= {
            "workflow",
            "ILab",
        }
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_child_updates_never_apply_parent_file_changed_before_watcher(
    tmp_path: Path,
) -> None:
    """子流程应用时不得触碰父 Python 文件或其未应用编辑。

    参数：``tmp_path`` 隔离真实领域包、内存定义和库存库。返回：无；断言刷新前
    直接修改的引用方文件保持未应用状态，子流程更新不会刷新父定义或覆盖用户
    编辑。异常：真实源码 CAS、编译或断言失败时由 pytest 报告。
    """

    reset_workflow_service_for_test()
    package_root = tmp_path / "editable"
    package_root.mkdir()
    _write_package(package_root)
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_package_roots=(package_root,),
        )
        parent_before = service.get_graph(PARENT_WORKFLOW_UUID)
        parent_revision = int(parent_before["workflow"]["revision"])
        parent_source_path = package_root / "c1_product_lab" / "workflows" / "parent.py"
        parent_source_path.write_text(
            parent_source_path.read_text(encoding="utf-8")
            + "\n# 用户尚未确认应用的编辑\n",
            encoding="utf-8",
        )

        result = _apply_source(
            service,
            CHILD_WORKFLOW_UUID,
            _child_source().replace("Published child", "Published child v3"),
        )

        warnings = result["apply_result"]["warnings"]
        assert warnings == []
        parent_after = service.get_graph(PARENT_WORKFLOW_UUID)
        assert int(parent_after["workflow"]["revision"]) == parent_revision
        # 只比较父工作流自身的已应用定义；读图中的模板目录投影会合法反映刚应用
        # 的子模板代际，但不能因此推进父修订或改写父节点/连线。
        assert parent_after["workflow"] == parent_before["workflow"]
        assert parent_after["nodes"] == parent_before["nodes"]
        assert parent_after["edges"] == parent_before["edges"]
        assert service.get_authoring(PARENT_WORKFLOW_UUID)["state"] in {
            "applied_source_stale",
            "unapplied_source_only",
            "unapplied_graph",
        }
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()
