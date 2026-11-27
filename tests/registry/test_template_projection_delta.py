"""模板投影差量（TemplateProjectionDelta）与持久发布合同测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.registry.test_template_projection import (
    RESOURCE_TEMPLATE_UUID,
    FakeRegistry,
)
from unilabos.registry.template_delta import (
    TemplateProjectionMember,
    build_template_projection_delta,
)
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.store import WorkflowStore


def _member(
    *,
    business_key: str,
    semantic_hash: str,
    generation: int,
) -> TemplateProjectionMember:
    """建立一个节点模板投影成员测试值。

    参数：``business_key`` 是模板稳定业务键；``semantic_hash`` 是规范语义摘要；
    ``generation`` 是该成员最后变化的投影代际。
    返回：权威、来源与目标身份均固定的模板投影成员。
    异常：字段不符合值对象约束时由 ``TemplateProjectionMember`` 原样抛出。
    """

    return TemplateProjectionMember(
        authority_id="local",
        projection_kind="node_template",
        source_definition_key=f"lab.devices:Pump#{business_key}",
        business_key=business_key,
        target_uuid=f"20000000-0000-4000-8000-{int(business_key):012d}",
        semantic_hash=semantic_hash,
        generation=generation,
    )


def _projection(database_path: Path) -> RegistryTemplateProjection:
    """装配使用确定资源模板身份的本地模板投影。

    参数：``database_path`` 是跨刷新复用的 SQLite 文件。
    返回：以 ``local`` 为投影权威（Authority）的注册表模板投影。
    异常：数据库或投影初始化错误原样传播，测试不得静默降级。
    """

    def resolve_resource_template_identity(resource_name: str) -> str:
        """解析本用例唯一资源模板业务名。

        参数：``resource_name`` 是动作合同声明的资源业务名。
        返回：``pump`` 的稳定 UUID；其他名称返回空串并由投影边界拒绝。
        异常：无。
        """

        return RESOURCE_TEMPLATE_UUID if resource_name == "pump" else ""

    return RegistryTemplateProjection(
        WorkflowStore(database_path),
        authority_id="local",
        resource_template_identity_resolver=resolve_resource_template_identity,
    )


def _durable_state(database_path: Path) -> dict[str, object]:
    """读取差量发布需要验证的持久事实。

    参数：``database_path`` 是模板投影使用的 SQLite 文件。
    返回：代际、节点/连接点时间戳和软删除状态，以及完整活动 manifest 行。
    异常：缺表或数据损坏原样传播，避免把不完整迁移误判为通过。
    """

    connection = sqlite3.connect(database_path)
    try:
        generation = connection.execute(
            """
            SELECT authority_id, generation, resource_template_symbols
            FROM registry_template_projection_generation
            ORDER BY authority_id
            """
        ).fetchall()
        nodes = connection.execute(
            """
            SELECT uuid, update_time, deleted_at, display_name
            FROM workflow_node_template
            ORDER BY uuid
            """
        ).fetchall()
        handles = connection.execute(
            """
            SELECT uuid, update_time, deleted_at, handle_key, io_type
            FROM workflow_handle_template
            ORDER BY uuid
            """
        ).fetchall()
        manifest = connection.execute(
            """
            SELECT authority_id, projection_kind, source_definition_key,
                   business_key, target_uuid, semantic_hash, generation
            FROM registry_template_projection_member
            ORDER BY projection_kind, business_key
            """
        ).fetchall()
    finally:
        connection.close()
    return {
        "generation": [tuple(row) for row in generation],
        "nodes": [tuple(row) for row in nodes],
        "handles": [tuple(row) for row in handles],
        "manifest": [tuple(row) for row in manifest],
    }


def test_delta_classifies_all_operations_and_only_advances_changed_members() -> None:
    """差量必须稳定区分新增、修改、移除和未变化成员。

    参数：无。
    返回：无；断言发生变化时只给新增/修改成员写入下一代际，未变化成员保留
    自身最后变化代际，移除成员保留删除前证据。
    异常：分类重叠、遗漏或无条件推进代际时断言失败。
    """

    # ``previous_members`` 是已提交第 2 代的权威 manifest 完整活动集合。
    previous_members = (
        _member(business_key="1", semantic_hash="sha256:old", generation=1),
        _member(business_key="2", semantic_hash="sha256:same", generation=2),
        _member(business_key="3", semantic_hash="sha256:removed", generation=2),
    )
    # ``candidate_members`` 是完整编译且已分配目标 UUID 的下一候选集合。
    candidate_members = (
        _member(business_key="1", semantic_hash="sha256:new", generation=0),
        _member(business_key="2", semantic_hash="sha256:same", generation=0),
        _member(business_key="4", semantic_hash="sha256:added", generation=0),
    )

    delta = build_template_projection_delta(
        authority_id="local",
        current_generation=2,
        previous_members=previous_members,
        candidate_members=candidate_members,
    )

    assert delta.generation == 3
    assert delta.changed is True
    assert [member.business_key for member in delta.added] == ["4"]
    assert [member.business_key for member in delta.modified] == ["1"]
    assert [member.business_key for member in delta.removed] == ["3"]
    assert [member.business_key for member in delta.unchanged] == ["2"]
    assert delta.added[0].generation == 3
    assert delta.modified[0].generation == 3
    assert delta.unchanged[0].generation == 2


def test_identical_refresh_is_noop_and_modified_refresh_touches_only_node(
    tmp_path: Path,
) -> None:
    """相同语义刷新不得推进代际或更新未变化模板时间。

    参数：``tmp_path`` 提供隔离的 SQLite 文件目录。
    返回：无；断言首次发布生成 manifest，完全相同刷新为 no-op，随后只修改动作
    展示字段时仅节点模板变化，连接点（Handle）保持原时间和成员代际。
    异常：重复 upsert、全量时间戳刷新或 manifest 分类错误时断言失败。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    projection.refresh(FakeRegistry(display_name="输送"))
    first_state = _durable_state(database_path)
    first_delta = projection.last_delta()

    projection.refresh(FakeRegistry(display_name="输送"))
    identical_state = _durable_state(database_path)
    identical_delta = projection.last_delta()

    assert first_delta.changed is True
    assert identical_delta.changed is False
    assert identical_delta.generation == first_delta.generation
    assert identical_state == first_state

    projection.refresh(FakeRegistry(display_name="转移"))
    modified_state = _durable_state(database_path)
    modified_delta = projection.last_delta()
    projection.close()

    assert modified_delta.generation == first_delta.generation + 1
    assert [member.projection_kind for member in modified_delta.modified] == [
        "node_template"
    ]
    assert modified_delta.added == ()
    assert modified_delta.removed == ()
    assert modified_state["generation"] != first_state["generation"]
    # ``first_handle_times`` 与 ``modified_handle_times`` 证明连接点完全未写入。
    first_handle_times = {
        (row[3], row[4]): row[1]
        for row in first_state["handles"]  # type: ignore[index]
    }
    modified_handle_times = {
        (row[3], row[4]): row[1]  # type: ignore[index]
        for row in modified_state["handles"]  # type: ignore[union-attr]
    }
    assert modified_handle_times == first_handle_times


def test_omitted_templates_are_soft_deleted_and_removed_from_active_manifest(
    tmp_path: Path,
) -> None:
    """完整候选遗漏的模板必须差量软删除并退出活动 manifest。

    参数：``tmp_path`` 提供隔离数据库。
    返回：无；断言节点和连接点进入软删除状态，差量报告移除成员，资源模板源码
    映射仍作为未变化成员保留。
    异常：硬删除、遗漏成员仍活动或无关成员被重写时断言失败。
    """

    database_path = tmp_path / "workflow_history.db"
    projection = _projection(database_path)
    projection.refresh(FakeRegistry())

    projection.refresh(FakeRegistry(include_action=False))
    removed_delta = projection.last_delta()
    state = _durable_state(database_path)
    projection.close()

    assert removed_delta.changed is True
    assert {member.projection_kind for member in removed_delta.removed} == {
        "node_template",
        "handle_template",
    }
    assert all(row[2] is not None for row in state["nodes"])  # type: ignore[index]
    assert all(row[2] is not None for row in state["handles"])  # type: ignore[index]
    assert all(
        row[1] == "resource_template_symbol"
        for row in state["manifest"]  # type: ignore[union-attr]
    )
