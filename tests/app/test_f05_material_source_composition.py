"""F05.1 物料来源（MaterialSource）生产组合根合同。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.registry.test_f05_material_source_catalog import (
    PLATE_SOURCE_IDENTITY,
    _Registry,
)
from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _applied_graph
from tests.workflow.test_f05_material_source_authoring import MATERIAL_SOURCE_NODE_UUID
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)

# ``SZLAB_MOUNT_UUID`` 是真实库存组合测试中 S3 空烧杯仓的实际物料（Material）身份。
SZLAB_MOUNT_UUID = "71000000-0000-4000-8000-000000000001"


def _szlab_material_source_code() -> str:
    """生成使用 SZLab 部署业务资源 ID 的物料来源作者源码。

    参数：无。返回：S3 空烧杯仓作为挂载资源的静态 Python 源码。异常：无；
    源码不创建工作流任务（WorkflowTask）或执行动作。
    """

    return f'''from lab.resources import plate_96
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    material_source,
    resource_ref,
    workflow,
    workflow_output,
)


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="SZLab mount")
def szlab_mount():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    plate = material_source(
        resource_template=plate_96,
        mode="existing",
        mount=resource_ref("s3_unused_beaker"),
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
    )
    return workflow_output()
'''


def _insert_resource_graph_material(
    inventory_store: InventoryStore,
    *,
    material_uuid: str,
    resource_template_uuid: str,
    source_node_id: str,
) -> None:
    """写入一条等价于本地资源图启动投影的活动物料事实。

    参数：``inventory_store`` 是隔离库存权威；``material_uuid`` 与
    ``resource_template_uuid`` 是实际稳定身份；``source_node_id`` 是部署资源图
    业务 ID。返回：无。异常：SQLite 约束错误直接传播；事务保证不留下部分行。
    """

    # ``metadata`` 保留 C3 资源图启动投影用于业务 ID 唯一解析的来源字段。
    metadata = json.dumps(
        {
            "source": "resource-tree-set",
            "source_node_id": source_node_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with inventory_store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO material(
                uuid, create_time, update_time, deleted_at, description, meta_data,
                resource_template_uuid, parent_uuid, class, barcode, name, config, data
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, ?, '', ?, '{}', '{}')
            """,
            (
                material_uuid,
                "2026-08-05T00:00:00.000Z",
                "2026-08-05T00:00:00.000Z",
                metadata,
                resource_template_uuid,
                "szlab.mount",
                f"{source_node_id}-{material_uuid}",
            ),
        )


def test_local_composition_shares_frozen_resource_template_projection(
    tmp_path: Path,
) -> None:
    """生产组合根应让创作编译器和模板查询共用同一冻结代际。

    参数说明：``tmp_path`` 是本地工作流/调度存储根目录。
    返回：无；断言组合后的稳定身份和模板共享。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是真实后端（Backend）形态库存模板写权威，启动前为空。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``service`` 与 ``projection`` 必须共享同一注册表（Registry）模板代际。
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
        )
        # ``template_rows`` 是库存权威提交的活动资源模板业务 ID/UUID 映射。
        template_rows = {
            str(row["name"]): str(row["uuid"])
            for row in inventory_store.query_all(
                """
                SELECT uuid, name
                FROM resource_template
                WHERE deleted_at IS NULL
                """
            )
        }

        assert service.compiler is not None
        assert service.compiler.template_catalog_fingerprint == (
            projection.snapshot().fingerprint
        )
        assert (
            projection.snapshot().require_resource_template_uuid(PLATE_SOURCE_IDENTITY)
            == template_rows["plate_96"]
        )
        assert (
            projection.snapshot()
            .require_material_source()
            .template["resource_template_uuid"]
            == template_rows["host_node"]
        )
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_creates_missing_material_template_identity(
    tmp_path: Path,
) -> None:
    """物料资源模板身份缺失时组合必须在库存权威中创建稳定身份。

    参数说明：``tmp_path`` 提供隔离存储。返回：无；断言工作流模板投影引用
    同一事务同步后 inventory.db 中的物料资源模板（ResourceTemplate）UUID。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是缺失模板身份首次创建的真实库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``projection`` 必须引用本次同步事务新建的反应板模板 UUID。
        _service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
        )
        # ``plate_row`` 是库存权威中唯一活动反应板资源模板事实。
        plate_row = inventory_store.query_one(
            """
            SELECT uuid
            FROM resource_template
            WHERE name = ? AND deleted_at IS NULL
            """,
            ("plate_96",),
        )

        assert plate_row is not None
        assert projection.snapshot().require_resource_template_uuid(
            PLATE_SOURCE_IDENTITY
        ) == str(plate_row["uuid"])
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_resolves_resource_graph_id_from_inventory(
    tmp_path: Path,
) -> None:
    """本地组合根必须从库存权威解析资源图业务 ID，而不是原样充当 UUID。

    参数：``tmp_path`` 隔离工作流和库存 SQLite。返回：无。断言：C3 形状的
    ``source_node_id`` 唯一映射进入物料来源（MaterialSource）候选选择器中的
    实际物料 UUID；本测试不创建工作流任务（WorkflowTask）或执行动作。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是包含资源图启动投影事实的本地库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``service`` 与 ``projection`` 来自正式本地产品组合接缝。
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
        )
        # ``mount_template_uuid`` 是本代宿主资源模板身份，仅用于构造有效物料行。
        mount_template_uuid = str(
            projection.snapshot().require_material_source().template[
                "resource_template_uuid"
            ]
        )
        _insert_resource_graph_material(
            inventory_store,
            material_uuid=SZLAB_MOUNT_UUID,
            resource_template_uuid=mount_template_uuid,
            source_node_id="s3_unused_beaker",
        )
        assert service.compiler is not None
        # ``compiled`` 只生成候选工作流图，不持久化工作流任务或动作执行。
        compiled = service.compiler.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            python_source=_szlab_material_source_code(),
            source_uri="package://szlab/workflows/szlab_mount.py",
            applied_graph=_applied_graph(),
        )

        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        # ``source_node`` 是唯一物料来源（MaterialSource）候选节点。
        source_node = compiled.graph["nodes"][0]
        assert source_node["param"]["mount"] == {"uuid": SZLAB_MOUNT_UUID}
        assert source_node["param"]["mount"]["uuid"] != "s3_unused_beaker"
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_rejects_ambiguous_resource_graph_id(
    tmp_path: Path,
) -> None:
    """同一资源图业务 ID 命中多个活动物料时本地组合必须失败关闭。

    参数：``tmp_path`` 隔离 SQLite。返回：无。断言：公共编译不产生候选图，
    只返回稳定资源解析错误；歧义库存不得触发工作流任务（WorkflowTask）或动作。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 故意包含两个相同 ``source_node_id`` 的活动物料事实。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``service`` 与 ``projection`` 仍经正式本地产品组合接缝创建。
        service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
        )
        # ``mount_template_uuid`` 是两个冲突物料共享的合法资源模板身份。
        mount_template_uuid = str(
            projection.snapshot().require_material_source().template[
                "resource_template_uuid"
            ]
        )
        for material_uuid in (
            SZLAB_MOUNT_UUID,
            "71000000-0000-4000-8000-000000000002",
        ):
            # ``material_uuid`` 是不同实际物料身份，故意共享同一部署业务 ID。
            _insert_resource_graph_material(
                inventory_store,
                material_uuid=material_uuid,
                resource_template_uuid=mount_template_uuid,
                source_node_id="s3_unused_beaker",
            )
        assert service.compiler is not None
        # ``compiled`` 必须把库存歧义收敛为稳定公共诊断。
        compiled = service.compiler.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            python_source=_szlab_material_source_code(),
            source_uri="package://szlab/workflows/ambiguous_mount.py",
            applied_graph=_applied_graph(),
        )

        assert not compiled.valid
        assert compiled.graph is None
        assert [item["code"] for item in compiled.diagnostics] == [
            "resource_reference_resolution_error"
        ]
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()
