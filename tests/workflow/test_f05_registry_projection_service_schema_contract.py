"""F05.4-C0c 注册表模板投影（Registry Template Projection）服务合同测试。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.registry.test_template_projection import (
    DEVICE_MATERIAL_UUID,
    RESOURCE_TEMPLATE_UUID,
    FakeRegistry,
)
from tests.workflow.test_authoring_engine import WORKFLOW_UUID
from unilabos.app.workflow_api import create_workflow_app
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

# ``ACTION_NODE_UUID`` 是真实注册动作（Action）在候选图（Candidate Graph）中的
# 稳定节点身份。
ACTION_NODE_UUID = "22000000-0000-4000-8000-000000000001"


class _StaticRegistry:
    """向注册表模板投影（Registry Template Projection）的深模块（Deep Module）提供可变输入。

    注册表模板投影（Registry Template Projection）的深模块（Deep Module）必须
    自行隔离调用方输入，不能把可变注册容器当作持久事实。
    """

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        """保存调用方持有的设备注册表（Registry）输入。

        参数说明：``devices`` 是设备注册表（Registry）完成构建后的设备定义集合。
        返回：无。异常：无；测试刻意保留同一容器，以验证注册表模板投影
        （Registry Template Projection）结果与输入深分离。
        """

        # ``self._devices`` 保留调用方设备注册表（Registry）输入的原始容器身份，
        # 用于证明注册表模板投影（Registry Template Projection）会自行深分离。
        self._devices = devices

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回调用方持有的设备定义集合。

        参数说明：无。返回：未复制的设备注册表（Registry）设备定义；注册表模板
        投影（Registry Template Projection）的深模块（Deep Module）必须自行建立
        快照。异常：无。
        """

        return self._devices

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回本用例不需要的空资源模板（ResourceTemplate）定义集合。

        参数说明：无。返回：空列表。异常：无。
        """

        return []


def _resolve_resource_template_uuid(resource_name: str) -> str:
    """把测试设备业务身份解析为稳定资源模板（ResourceTemplate）UUID。

    参数说明：``resource_name`` 来自注册设备定义。返回：``pump`` 对应的规范
    UUID；未知身份返回空字符串，使真实投影按关闭失败（Fail-closed）规则拒绝。
    异常：无。
    """

    return RESOURCE_TEMPLATE_UUID if resource_name == "pump" else ""


def _workflow_source() -> str:
    """生成调用真实注册动作的可信工作流源码（Workflow Source）。

    参数说明：无。返回：含固定执行器（Fixed Executor）、数字参数和稳定节点身份
    的 Python 源码；该源码可经过公共候选版本（Candidate）签发和应用链。异常：
    无。
    """

    return f'''from lab.devices import Pump
from unilabos.workflow.authoring import device, workflow, workflow_output


pump: Pump = device("{DEVICE_MATERIAL_UUID}")


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Registry schema service contract",
)
def registry_schema_service_contract():
    # unilab:node_uuid={ACTION_NODE_UUID}
    accepted = pump.transfer(volume=1.5)
    return workflow_output()
'''


def test_registry_schema_is_backend_string_through_candidate_apply_and_restart(
    tmp_path: Path,
) -> None:
    """真实注册参数 Schema 必须以后端（Backend）字符串形状完成签发、应用与重读。

    参数说明：``tmp_path`` 隔离工作流（Workflow）/调度器（Scheduler）存储和
    可编辑包（Editable Package）。返回：无。异常/断言：注册表模板投影
    （Registry Template Projection）若共享输入容器、重启后恢复为 ``dict``，
    或工作流服务（WorkflowService）无法签发、保存及以 HTTP 读取同一字符串
    Schema，测试失败。
    """

    # ``database_path`` 是跨注册表模板投影（Registry Template Projection）和
    # 工作流服务（WorkflowService）重启共享的本地工作流（Workflow）事实文件。
    database_path = tmp_path / "workflow_history.db"
    # ``registry_devices`` 是调用方仍可修改的设备注册表（Registry）输入，用来
    # 证明注册表模板投影（Registry Template Projection）边界深分离。
    registry_devices = FakeRegistry().obtain_registry_device_info()
    # ``goal_schema`` 是后端（Backend）`workflow_node_template.schema` 表达的 goal
    # 参数子模式副本，后续注册输入变更不得污染它。
    goal_schema = deepcopy(
        registry_devices[0]["class"]["action_value_mappings"]["transfer"]["schema"][
            "properties"
        ]["goal"]
    )
    # ``expected_schema_text`` 按后端（Backend）文本列语义确定性编码参数 Schema，
    # 是投影、候选版本（Candidate）和持久重读共同使用的预期值。
    expected_schema_text = json.dumps(
        goal_schema,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    # ``first_projection`` 是首次发布可变设备注册表（Registry）输入的注册表模板
    # 投影（Registry Template Projection）实例。
    first_projection = RegistryTemplateProjection(
        WorkflowStore(database_path),
        authority_id="f05-c0c",
        resource_template_identity_resolver=_resolve_resource_template_uuid,
    )
    # ``first_snapshot`` 是首次原子发布后与注册输入深分离的目录快照（Catalog
    # Snapshot）。
    first_snapshot = first_projection.refresh(_StaticRegistry(registry_devices))
    # ``first_action`` 是首次目录快照（Catalog Snapshot）中按设备类和动作业务名
    # 解析的动作模板（Action Template）。
    first_action = first_snapshot.require_action("lab.devices:Pump", "transfer")
    assert first_action.template["schema"] == expected_schema_text
    first_projection.close()

    # 修改调用方仍持有的设备注册表（Registry）输入，模拟注册表模板投影
    # （Registry Template Projection）提交后的外部容器漂移；已提交目录快照
    # （Catalog Snapshot）和 SQLite 事实必须保持不变。
    registry_devices[0]["class"]["action_value_mappings"]["transfer"]["schema"][
        "properties"
    ]["goal"]["properties"]["volume"]["type"] = "string"

    # ``restarted_projection`` 是从同一 SQLite 事实恢复、但不再读取原注册输入的
    # 注册表模板投影（Registry Template Projection）实例。
    restarted_projection = RegistryTemplateProjection(
        WorkflowStore(database_path),
        authority_id="f05-c0c",
        resource_template_identity_resolver=_resolve_resource_template_uuid,
    )
    # ``restarted_action`` 是重启恢复目录快照（Catalog Snapshot）中的同一动作
    # 模板（Action Template），用于证明 Schema 文本跨进程生命周期不变。
    restarted_action = restarted_projection.snapshot().require_action(
        "lab.devices:Pump",
        "transfer",
    )
    assert restarted_action.template["schema"] == expected_schema_text

    # ``service_store`` 与已恢复注册表模板投影（Registry Template Projection）
    # 读取同一 SQLite 事实，但拥有独立服务连接。
    service_store = WorkflowStore(database_path)
    # ``service`` 是使用恢复目录快照签发并应用候选版本（Candidate）的工作流服务
    # （WorkflowService）。
    service = WorkflowService(
        service_store,
        compiler=WorkflowAuthoringEngine(catalog=restarted_projection.snapshot()),
    )
    # ``package_root`` 是本测试显式授权的可编辑包（Editable Package）根目录。
    package_root = tmp_path / "package"
    # ``source_path`` 是与被测工作流（Workflow）稳定关联的工作流源码（Workflow
    # Source）文件路径。
    source_path = package_root / "workflows" / "schema_contract.py"
    source_path.parent.mkdir(parents=True)
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Registry schema service contract",
        tags=[],
        description=None,
        meta_data={},
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="lab",
        package_root=package_root,
        relative_path="workflows/schema_contract.py",
    )
    try:
        # ``draft`` 是保存工作流源码（Workflow Source）后返回、携带已持久候选版本
        # （Candidate）的工作流创作聚合。
        draft = service.save_draft(
            WORKFLOW_UUID,
            python_source=_workflow_source(),
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        # ``candidate`` 是服务已签发并持久化的候选版本（Candidate）返回副本。
        candidate = draft["candidate"]
        assert candidate is not None, draft["draft"]["diagnostics"]
        assert candidate["graph"]["node_templates"][0]["schema"] == (
            expected_schema_text
        )

        candidate["graph"]["node_templates"][0]["schema"] = "false"
        # ``stored_candidate`` 是重新读取的候选版本（Candidate）持久事实，不能与
        # 调用方返回值共享容器。
        stored_candidate = service.get_authoring(WORKFLOW_UUID)["candidate"]
        assert stored_candidate is not None
        assert stored_candidate["graph"]["node_templates"][0]["schema"] == (
            expected_schema_text
        )

        service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=stored_candidate["candidate_hash"],
        )
        # ``graph_response`` 是应用候选版本（Candidate）后通过后端形态契约
        # （Backend-shaped Contract）读取工作流图（Workflow Graph）的 HTTP
        # 响应。
        graph_response = TestClient(create_workflow_app(service)).get(
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph"
        )
        assert graph_response.status_code == 200
        assert graph_response.json()["data"]["node_templates"][0]["schema"] == (
            expected_schema_text
        )
    finally:
        service.close()
        restarted_projection.close()

    # ``reopened_store`` 是工作流服务（WorkflowService）和注册表模板投影
    # （Registry Template Projection）均关闭后再次打开的本地工作流（Workflow）
    # 事实存储，用于证明已应用工作流图（Applied Workflow Graph）的 Schema
    # 字符串跨重启保持不变。
    reopened_store = WorkflowStore(database_path)
    try:
        assert (
            reopened_store.get_graph(WORKFLOW_UUID)["node_templates"][0]["schema"]
            == expected_schema_text
        )
    finally:
        reopened_store.close()
