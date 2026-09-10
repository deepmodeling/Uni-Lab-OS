"""F05.4-A 物料来源（MaterialSource）公开 HTTP 调度纵向合同。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.app.f05_material_source_http_fixture import (
    SchedulerGetter,
    install_applied_graph,
)
from unilabos.app.scheduler.api import create_scheduler_router
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

# 以下 UUID 分别代表夹具占用预留的工作流任务（WorkflowTask）与消费节点身份。
HOLDER_TASK_UUID = "61000000-0000-4000-8000-000000000404"
HOLDER_NODE_UUID = "61000000-0000-4000-8000-000000000405"


@dataclass(slots=True)
class _Runtime:
    """汇总真实本地 HTTP 运行时的公开客户端、两类权威与可观察派发边界。"""

    client: TestClient
    workflow_store: WorkflowStore
    inventory: InventoryService
    scheduler: EdgeScheduler
    dispatcher: RecordingDispatcher


def _create_resource_template(
    client: TestClient,
    *,
    resource_id: str,
    display_name: str,
    registry_type: str,
) -> str:
    """通过公开 HTTP 创建物料使用的资源模板（ResourceTemplate）。

    参数：``client`` 是组合应用客户端；其余字段声明业务身份、显示名和资源类型。
    返回：服务端生成的稳定模板 UUID。异常：公开接口失败由断言终止夹具，禁止
    绕过合同直接写库存数据库。
    """

    response = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": resource_id,
                    "display_name": display_name,
                    "registry_type": registry_type,
                    "model": {},
                    "class": {"module": "lab.plate", "type": "python"},
                    "handles": [],
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["code"] == 0
    return str(response.json()["data"]["templates"][0]["uuid"])


def _create_material(
    client: TestClient,
    *,
    resource_template_uuid: str,
    barcode: str,
    name: str,
    parent_uuid: str | None = None,
    site_uuid: str | None = None,
) -> str:
    """通过公开 HTTP 创建具体物料（Material）。

    参数：客户端提交所属资源模板、条码和名称。返回：服务端生成的稳定物料 UUID。
    异常：接口未返回 201/成功 envelope 时由断言失败，不直接写库存数据库。
    """

    payload = {
        "resource_template_uuid": resource_template_uuid,
        "barcode": barcode,
        "name": name,
    }
    if parent_uuid is not None:
        payload["parent_uuid"] = parent_uuid
    if site_uuid is not None:
        payload["site_placement"] = {"action": "place", "site_uuid": site_uuid}
    response = client.post(
        "/api/v1/materials",
        json=payload,
    )
    assert response.status_code == 201
    assert response.json()["code"] == 0
    return str(response.json()["data"]["uuid"])


def _apply_workflow_graph(
    runtime: _Runtime,
    *,
    material_resource_template_uuid: str,
    device_resource_template_uuid: str,
    material_uuid: str,
    device_material_uuid: str,
    mode: str,
    automatic: bool = False,
    site_uuid: str | None = None,
    slot_uuids: list[str] | None = None,
) -> str:
    """通过共享正式夹具保存本轮物料来源工作流图。

    参数：``runtime`` 持有唯一工作流权威；两类资源模板和物料 UUID 分别冻结来源
    与执行设备，``mode`` 切换失败关闭反例。返回：公开创建并保存真实图的工作流
    UUID。异常：模板投影、图保存或公开接口失败直接传播；不替换生产私有方法。
    """

    return install_applied_graph(
        runtime.client,
        runtime.workflow_store,
        material_resource_template_uuid=material_resource_template_uuid,
        device_resource_template_uuid=device_resource_template_uuid,
        material_uuid=material_uuid,
        device_material_uuid=device_material_uuid,
        mode=mode,
        automatic=automatic,
        site_uuid=site_uuid,
        slot_uuids=slot_uuids,
    )


@pytest.fixture()
def runtime(tmp_path: Path) -> Iterator[_Runtime]:
    """装配真实库存、调度桥、工作流服务与组合 FastAPI 应用。

    参数：``tmp_path`` 隔离两类 SQLite 权威。产生：共享同一库存服务和调度器的
    运行时；结束时关闭桥、工作流存储及库存存储。异常：装配失败原样传播。
    """

    # ``inventory_store`` 是本测试唯一库存权威（Inventory Authority）。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    inventory = InventoryService(inventory_store)
    # ``workflow_store`` 是工作流任务（WorkflowTask）和工作流节点作业
    # （WorkflowNodeJob）的唯一工作流权威。
    workflow_store = WorkflowStore(tmp_path / "workflow_history.db")
    # ``dispatcher`` 记录是否越过真实设备派发边界，不承担领域状态权威。
    dispatcher = RecordingDispatcher()
    # ``scheduler`` 复用真实库存服务并负责本地工作流任务
    # （WorkflowTask）推进。
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    # ``bridge`` 将工作流任务（WorkflowTask）/工作流节点作业
    # （WorkflowNodeJob）状态投影与同一本地调度器（Scheduler）绑定。
    bridge = TaskSchedulerBridge(workflow_store, scheduler=scheduler)
    # ``workflow_service`` 是公开工作流（Workflow）HTTP 路由使用的
    # 唯一应用服务。
    workflow_service = WorkflowService(
        workflow_store,
        task_scheduler_bridge=bridge,
    )
    app = create_workflow_app(workflow_service)
    install_backend_resource_api(app, BackendResourceService(inventory_store))
    app.include_router(
        create_scheduler_router(
            SchedulerGetter(scheduler),
            include_execution_shaped_workflow_routes=False,
        )
    )
    try:
        with TestClient(app) as client:
            yield _Runtime(
                client=client,
                workflow_store=workflow_store,
                inventory=inventory,
                scheduler=scheduler,
                dispatcher=dispatcher,
            )
    finally:
        workflow_service.close()
        inventory_store.close()


def test_fixed_existing_waits_then_reschedules_with_same_task_and_job(
    runtime: _Runtime,
) -> None:
    """固定既有物料被占用时等待，释放后以同一身份完成准入重试。

    参数：``runtime`` 是真实组合运行时。返回无；断言公开 HTTP 创建物料和
    工作流任务（WorkflowTask），外部工作流任务（WorkflowTask）/工作流
    节点作业（WorkflowNodeJob）保持 ``pending``，且不向旧调度器注册普通动作；
    释放夹具占用后，``POST /reschedule`` 使同一工作流任务（WorkflowTask）和两项
    工作流节点作业（WorkflowNodeJob）继续推进。异常：任何私有替代接口、新工作流
    任务（WorkflowTask）身份或提前设备派发都会使断言失败。
    """

    # ``material_template_uuid`` 是来源物料 API 与模板投影共享的类型身份。
    material_template_uuid = _create_resource_template(
        runtime.client,
        resource_id="lab.plate",
        display_name="96 孔板",
        registry_type="material",
    )
    # ``device_template_uuid`` 是执行设备物料与 ILab 模板共享的类型身份。
    device_template_uuid = _create_resource_template(
        runtime.client,
        resource_id="lab.reactor",
        display_name="反应器",
        registry_type="device",
    )
    # ``material_uuid`` 是 HTTP、冻结选择器和短期遗留预留共享的稳定物料身份。
    material_uuid = _create_material(
        runtime.client,
        resource_template_uuid=material_template_uuid,
        barcode="F05-PLATE-001",
        name="F05 固定孔板",
    )
    # ``device_material_uuid`` 是动作节点绑定的实际执行设备物料身份。
    device_material_uuid = _create_material(
        runtime.client,
        resource_template_uuid=device_template_uuid,
        barcode="F05-REACTOR-001",
        name="F05 反应器",
    )
    # ``workflow_uuid`` 是公开定义、图和工作流任务（WorkflowTask）三条接口
    # 共享的工作流（Workflow）身份。
    workflow_uuid = _apply_workflow_graph(
        runtime,
        material_resource_template_uuid=material_template_uuid,
        device_resource_template_uuid=device_template_uuid,
        material_uuid=material_uuid,
        device_material_uuid=device_material_uuid,
        mode="existing",
    )
    runtime.inventory.reserve_workflow(
        HOLDER_TASK_UUID,
        {HOLDER_NODE_UUID: [MaterialRequirement(instance_uuid=material_uuid)]},
    )

    created = runtime.client.post(
        "/api/v1/workflow-tasks",
        json={"workflow_uuid": workflow_uuid, "run_mode": "normal", "meta_data": {}},
    )
    assert created.status_code == 201, created.json()
    assert created.json()["code"] == 0
    # ``task_uuid`` 与下方工作流节点作业（WorkflowNodeJob）UUID 是本次
    # 准入重试（AdmissionRetry）前后必须保持不变的持久身份。
    task_uuid = str(created.json()["data"]["uuid"])
    jobs_before = runtime.client.get(f"/api/v1/workflow-tasks/{task_uuid}/jobs").json()[
        "data"
    ]

    assert created.json()["data"]["status"] == "pending"
    assert [job["executor_kind"] for job in jobs_before] == [
        "material_source",
        "device_action",
    ]
    assert [job["status"] for job in jobs_before] == ["pending", "pending"]
    assert runtime.dispatcher.dispatched == []
    assert runtime.client.get("/api/v1/materials/graph").json()["code"] == 0
    assert runtime.scheduler.workflow_snapshot(task_uuid) is None

    runtime.inventory.release_workflow(HOLDER_TASK_UUID, reason="fixture_release")
    rescheduled = runtime.client.post("/api/v1/reschedule")
    jobs_after = runtime.client.get(f"/api/v1/workflow-tasks/{task_uuid}/jobs").json()[
        "data"
    ]
    task_after = runtime.client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()[
        "data"
    ]

    assert rescheduled.status_code == 200
    assert task_after["uuid"] == task_uuid
    assert task_after["status"] == "running"
    assert [job["uuid"] for job in jobs_after] == [
        job["uuid"] for job in jobs_before
    ]
    assert [job["status"] for job in jobs_after] == ["succeeded", "dispatched"]
    assert len(runtime.dispatcher.dispatched) == 1


def test_create_new_fails_closed_without_task_or_material_graph_change(
    runtime: _Runtime,
) -> None:
    """短期 ``create_new`` 应在工作流任务（WorkflowTask）事务内失败关闭并保持物料图不变。

    参数：``runtime`` 是真实组合运行时。返回无；断言公开工作流任务
    （WorkflowTask）接口返回 Backend 业务错误，工作流任务（WorkflowTask）列表零新增，前后公开
    物料图完全相同且没有设备派发。异常：若不支持模式创建了部分工作流任务
    （WorkflowTask）、
    物料或工作流节点作业（WorkflowNodeJob），任一公共响应断言都会失败。
    """

    # ``material_template_uuid`` 是 create_new 图声明的来源资源类型身份。
    material_template_uuid = _create_resource_template(
        runtime.client,
        resource_id="lab.plate",
        display_name="96 孔板",
        registry_type="material",
    )
    # ``device_template_uuid`` 是 ILab 动作模板和执行设备共享的类型身份。
    device_template_uuid = _create_resource_template(
        runtime.client,
        resource_id="lab.reactor",
        display_name="反应器",
        registry_type="device",
    )
    # ``mount_material_uuid`` 只满足物料来源选择器的挂载引用，不是待新建物料。
    mount_material_uuid = _create_material(
        runtime.client,
        resource_template_uuid=material_template_uuid,
        barcode="F05-MOUNT-001",
        name="F05 新建来源挂载物料",
    )
    # ``device_material_uuid`` 是 create_new 图仍需绑定的实际执行设备物料身份。
    device_material_uuid = _create_material(
        runtime.client,
        resource_template_uuid=device_template_uuid,
        barcode="F05-REACTOR-002",
        name="F05 create_new 反应器",
    )
    # ``workflow_uuid`` 是失败关闭前已经公开持久化的工作流定义身份。
    workflow_uuid = _apply_workflow_graph(
        runtime,
        material_resource_template_uuid=material_template_uuid,
        device_resource_template_uuid=device_template_uuid,
        material_uuid=mount_material_uuid,
        device_material_uuid=device_material_uuid,
        mode="create_new",
    )
    material_graph_before = runtime.client.get("/api/v1/materials/graph").json()
    tasks_before = runtime.client.get("/api/v1/workflow-tasks").json()["data"]

    failed = runtime.client.post(
        "/api/v1/workflow-tasks",
        json={"workflow_uuid": workflow_uuid, "run_mode": "normal", "meta_data": {}},
    )

    material_graph_after = runtime.client.get("/api/v1/materials/graph").json()
    tasks_after = runtime.client.get("/api/v1/workflow-tasks").json()["data"]
    assert failed.status_code == 200
    assert failed.json()["code"] == 1000
    assert failed.json()["error"]["msg"]
    assert tasks_after == tasks_before
    assert material_graph_after == material_graph_before
    assert runtime.dispatcher.dispatched == []
