"""QG01 宿主节点（HostNode）物料转运动作的可信创作合同。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.registry.registry import Registry
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.ros.nodes.presets.host_node import HostNode, _dump_resource_slot
from unilabos.workflow.store import WorkflowStore

HOST_RESOURCE_TEMPLATE_UUID = "90000000-0000-4000-8000-000000000001"


def _host_resource_template_identity(_registry_identity: str) -> str:
    """把本测试唯一宿主注册表身份解析为稳定资源模板（ResourceTemplate）UUID。

    参数：``_registry_identity`` 是真实注册表投影请求的宿主业务身份；当前夹具
    只有 ``host_node``，故该值不参与分支选择。返回：预先固定的宿主资源模板
    UUID。异常：无；测试意在验证动作合同而非库存同步。
    """

    return HOST_RESOURCE_TEMPLATE_UUID


def test_builtin_host_transfer_is_projected_with_material_lock_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置转运必须发布可供 SZLab 编译的类型化动作（Typed Action）合同。

    参数：``tmp_path`` 隔离注册表模板投影（Registry Template Projection）的
    SQLite；``monkeypatch`` 隔离全局注册表（Registry）单例的既有代际。返回：
    无；断言真实内置扫描、持久模板投影和工作流创作编译器共同识别
    ``host_node.transfer_resource``，完整连接点（Handle）可查询，且两个物料
    输入默认声明动作物料锁（Action Material Lock）。异常：模板缺失或合同无效
    时测试保持 RED，并禁止通过猜测动作身份继续编译。
    """

    # ``registry`` 是只扫描核心 HostNode 源码的真实注册表代际，不伪造动作 DTO。
    registry = Registry()
    monkeypatch.setattr(registry, "_setup_called", False)
    monkeypatch.setattr(registry, "_startup_executor", None)
    monkeypatch.setattr(registry, "device_type_registry", {})
    monkeypatch.setattr(registry, "resource_type_registry", {})
    registry.setup(external_only=True)
    # ``action_mapping`` 是设备传输层使用的动作参数、占位符和免排队标记；工作流
    # 创作连接点由下方 canonical schema 投影，避免双份合同漂移。
    action_mapping = registry.device_type_registry["host_node"]["class"][
        "action_value_mappings"
    ]["transfer_resource"]
    assert action_mapping["goal"] == {
        "resource": "resource",
        "target_device": "target_device",
        "mount_resource": "mount_resource",
        "site": "site",
        "site_uuid": "site_uuid",
    }
    assert action_mapping["placeholder_keys"] == {
        "resource": "unilabos_resources",
        "target_device": "unilabos_devices",
        "mount_resource": "unilabos_nodes",
    }
    assert action_mapping["always_free"] is True
    assert action_mapping["node_type"] == "ILab"
    assert action_mapping["executor_kind"] == "material_transfer"

    # ``projection`` 是工作流创作使用的持久模板权威；宿主资源模板 UUID 模拟已由
    # 库存权威（Inventory Authority）稳定解析，测试不创建第二套身份。
    projection = RegistryTemplateProjection(
        WorkflowStore(tmp_path / "workflow_history.db"),
        authority_id="qg01-host-transfer",
        resource_template_identity_resolver=_host_resource_template_identity,
    )
    try:
        catalog = projection.refresh(registry)
        transfer = catalog.require_action(
            "unilabos.ros.nodes.presets.host_node:HostNode",
            "transfer_resource",
        )
        transfer_template = transfer.detached_template()
    finally:
        projection.close()

    assert transfer_template["class"] == (
        "unilabos.ros.nodes.presets.host_node:HostNode"
    )
    assert transfer_template["node_type"] == "ILab"
    assert transfer_template["meta_data"]["unilab"]["contract_kind"] == "typed"
    assert transfer_template["meta_data"]["unilab"]["always_free"] is True
    assert (
        transfer_template["meta_data"]["unilab"]["executor_kind"]
        == "material_transfer"
    )
    goal_properties = transfer_template["meta_data"]["unilab"][
        "action_contract_schema"
    ]["properties"]["goal"]["properties"]
    assert goal_properties["resource"]["x-unilabos-material-lock"] is True
    assert goal_properties["mount_resource"]["x-unilabos-material-lock"] is True
    assert goal_properties["site_uuid"]["type"] == "string"
    assert "site_uuid" not in transfer_template["meta_data"]["unilab"][
        "action_contract_schema"
    ]["properties"]["goal"].get("required", [])
    assert "x-unilabos-site-selector" not in goal_properties["site_uuid"]
    # ``handles`` 是 SZLab 工作流源码（Workflow Source）静态编译时唯一可用的
    # 输入、输出与依赖端口全集，不能依赖已经删除的旧装饰器 handles。
    handles = {
        (str(handle["handle_key"]), str(handle["io_type"])): handle
        for handle in transfer.handles
    }
    assert {
        ("resource", "target"),
        ("resource", "source"),
        ("mount_resource", "target"),
        ("mount_resource", "source"),
        ("site", "target"),
        ("site", "source"),
        ("site_uuid", "target"),
        ("target_device", "target"),
        ("ready", "target"),
        ("ready", "source"),
    } <= set(handles)


class _TransferRuntime:
    """模拟保留执行核心的宿主节点（HostNode）直接调用接缝。"""

    def __init__(self) -> None:
        """初始化尚未收到调用的直接运行记录。

        参数：无。返回：无。异常：无；``call`` 只保存本测试观察的参数顺序。
        """

        self.call: tuple[Any, str, Any, str, str] | None = None

    async def _do_transfer_resource(
        self,
        resource: Any,
        target_device: str,
        mount_resource: Any,
        site: str,
        site_uuid: str,
    ) -> dict[str, Any]:
        """记录转运参数并返回规范 ResourceSlot 四键结果字典。

        参数：``resource`` 与 ``mount_resource`` 是待转移物料和目标父物料；
        ``target_device`` 是目标设备身份；``site`` 是目标库位（Site）名；
        ``site_uuid`` 是可选稳定身份。返回：
        保留 ``resource/mount_resource/site/result`` 的四键形状。异常：无；
        本替身不触发物理动作或物料权威写入。
        """

        self.call = (resource, target_device, mount_resource, site, site_uuid)
        return {
            "resource": {"uuid": "resource-uuid"},
            "mount_resource": {"uuid": "mount-uuid"},
            "site": site,
            "result": "转运完成",
        }


class _TransferExecutionRuntime:
    """模拟执行核心并记录物理转运与库存提交的先后顺序。"""

    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        """保存共享调用记录；参数 ``calls`` 供库存替身共同追加事实。"""

        self.calls = calls

    def lab_logger(self) -> logging.Logger:
        """返回测试日志器，覆盖兼容降级路径的告警接缝。

        参数：无。返回：当前测试模块日志器。异常：无。
        """

        return logging.getLogger(__name__)

    async def transfer_resource_to_another(
        self,
        resources: list[Any],
        target_device: str,
        mount_resources: list[Any],
        sites: list[str | None],
    ) -> str:
        """记录既有本地资源树转运并返回成功结果。"""

        self.calls.append(
            ("resource_tree", resources, target_device, mount_resources, sites)
        )
        return "转运完成"


class _InventoryTransferRecorder:
    """记录主机动作提交到边缘库存权威（Inventory Authority）的移动事实。"""

    def __init__(
        self,
        calls: list[tuple[Any, ...]],
        *,
        site_row: dict[str, Any] | None = None,
    ) -> None:
        """保存共享记录与可选库位查询结果。

        参数：``calls`` 用于验证提交顺序；``site_row`` 非空时同时模拟库存存储
        的库位权威查询。返回：无。异常：无。
        """

        self.calls = calls
        self._site_row = site_row
        if site_row is not None:
            self.store = self

    def query_one(self, _sql: str, _params: tuple[Any, ...]) -> dict[str, Any] | None:
        """返回预置库位行，供 ``site_uuid`` 直接调用路径解析。

        参数：SQL 与参数只保持库存查询接口形状，本替身不解释其内容。返回：
        预置库位行或 ``None``。异常：无。
        """

        return self._site_row

    def move_instance(
        self,
        edge_uuid: str,
        parent_uuid: str,
        slot_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        """记录物料（Material）新父级与目标库位（Site）并返回版本事实。"""

        self.calls.append(
            ("inventory", edge_uuid, parent_uuid, slot_id, actor)
        )
        return {"edge_uuid": edge_uuid, "version": 9}


def test_typed_host_transfer_preserves_direct_runtime_call_shape() -> None:
    """类型化动作（Typed Action）必须保留调用参数和规范四键结果字典。

    参数：无。返回：无；断言装饰器包装后的公开方法仍把五个参数原样交给既有
    执行核心，并把规范 ResourceSlot 结果对象原样返回。异常：本测试不执行实际设备
    动作；若包装层改写参数或结果则断言失败。
    """

    async def _call() -> tuple[Any, object, object, dict[str, Any]]:
        # ``runtime`` 是不具备 ROS2 能力的最小直接调用接缝，证明本轮没有改动执行核心。
        runtime = _TransferRuntime()
        # 两个对象分别代表待转移物料与目标父物料；这里只验证稳定传参，不解释内容。
        resource = object()
        mount_resource = object()
        result = await HostNode.transfer_resource(
            runtime,
            resource,
            "target-device",
            mount_resource,
            "L1B1",
        )
        return runtime, resource, mount_resource, result

    runtime, resource, mount_resource, result = asyncio.run(_call())

    assert runtime.call == (resource, "target-device", mount_resource, "L1B1", "")
    assert result == {
        "resource": {"uuid": "resource-uuid"},
        "mount_resource": {"uuid": "mount-uuid"},
        "site": "L1B1",
        "result": "转运完成",
    }


def test_host_transfer_never_reads_or_writes_scheduler_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime 只能执行本地资源树动作并返回回执，不能访问调度库存。

    参数：``monkeypatch`` 把任何库存服务读取都变成测试失败。返回：无；断言只执行
    Scheduler 已经解析好的规范库位名，并返回设备执行回执。异常：Runtime 查询或写入
    Inventory Authority 时立即失败，防止跨进程越权修改调度物理事实。
    """

    calls: list[tuple[Any, ...]] = []

    def _forbid_inventory_access() -> Any:
        raise AssertionError("Edge Runtime must not access scheduler inventory")

    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        _forbid_inventory_access,
    )
    runtime = _TransferExecutionRuntime(calls)
    resource = {"uuid": "10000000-0000-4000-8000-000000000001"}
    mount = {"uuid": "20000000-0000-4000-8000-000000000002"}

    result = asyncio.run(
        HostNode._do_transfer_resource(
            runtime,
            resource,
            "host_node",
            mount,
            "L1B1",
            "30000000-0000-4000-8000-000000000003",
        )
    )

    assert calls == [
        ("resource_tree", [resource], "host_node", [mount], ["L1B1"]),
    ]
    assert result["result"] == "转运完成"
    assert result["site"] == "L1B1"


def test_host_transfer_rejects_site_uuid_without_canonical_site_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduler 未提供规范库位名时 Runtime 必须失败关闭且不能反查库存。

    参数：``monkeypatch`` 禁止库存服务读取。返回：无；断言只有 ``site_uuid`` 的
    非规范派发被拒绝，要求 Scheduler 在 Job payload 中同时下发库位名。异常：任何
    库存访问都使测试失败。
    """

    calls: list[tuple[Any, ...]] = []
    resource_uuid = "10000000-0000-4000-8000-000000000001"
    owner_uuid = "20000000-0000-4000-8000-000000000002"
    site_uuid = "30000000-0000-4000-8000-000000000003"

    def _forbid_inventory_access() -> Any:
        raise AssertionError("Edge Runtime must not access scheduler inventory")

    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        _forbid_inventory_access,
    )
    runtime = _TransferExecutionRuntime(calls)

    with pytest.raises(ValueError, match="规范库位名"):
        asyncio.run(
            HostNode._do_transfer_resource(
                runtime,
                {"uuid": resource_uuid},
                "host_node",
                {"uuid": owner_uuid},
                site_uuid=site_uuid,
            )
        )
    assert calls == []


def test_transfer_runtime_prefers_explicit_site_over_legacy_extra() -> None:
    """验证显式库位不会被物料上的旧兼容提示覆盖。

    参数：无。返回：无。异常：若资源树执行使用 ``update_resource_site`` 覆盖
    已校验的显式 ``site``，断言报告目标库位身份漂移。
    """

    class _ParentResource:
        """记录最终传给 PLR 父物料的 ``spot``。"""

        def __init__(self) -> None:
            """初始化尚未分配子物料的父物料替身。

            参数：无。返回：无。异常：无。
            """

            self._ordering: dict[str, Any] = {}
            self.received_spot: Any = None

        def assign_child_resource(
            self,
            _resource: Any,
            location: Any = None,
            *,
            spot: Any = None,
        ) -> None:
            """记录显式库位参数。

            参数：物料与 ``location`` 仅保持 PLR 接口形状；``spot`` 是最终库位。
            返回：无。异常：无。
            """

            del location
            self.received_spot = spot

    parent = _ParentResource()
    material = SimpleNamespace(
        name="material",
        parent=None,
        unilabos_extra={"update_resource_site": "LEGACY-SITE"},
    )
    runtime = SimpleNamespace(
        uuid="host-node",
        identifier="host-node",
        lab_logger=lambda: logging.getLogger(__name__),
        resource_tracker=SimpleNamespace(
            uuid_to_resources={"parent-uuid": parent},
            resources=[],
        ),
        driver_instance=SimpleNamespace(),
    )
    tree = SimpleNamespace(
        root_node=SimpleNamespace(
            res_content=SimpleNamespace(
                parent_uuid="parent-uuid",
                name="parent",
            )
        )
    )

    result = BaseROS2DeviceNode.transfer_to_new_resource(
        runtime,
        material,
        tree,
        {"site": "EXPLICIT-SITE"},
    )

    assert result is parent
    assert parent.received_spot == "EXPLICIT-SITE"


def test_host_transfer_preserves_scheduler_resolved_legacy_site_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅有库位名的旧 Job 仍可执行，但 Runtime 不读取或提交库存。

    参数：``monkeypatch`` 禁止库存访问。返回：无；断言 Scheduler 下发的旧库位名
    原样传给设备资源树。异常：Runtime 尝试查库存时测试失败。
    """

    calls: list[tuple[Any, ...]] = []

    def _forbid_inventory_access() -> Any:
        raise AssertionError("Edge Runtime must not access scheduler inventory")

    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        _forbid_inventory_access,
    )
    runtime = _TransferExecutionRuntime(calls)

    result = asyncio.run(
        HostNode._do_transfer_resource(
            runtime,
            {"uuid": "10000000-0000-4000-8000-000000000001"},
            "host_node",
            {"uuid": "20000000-0000-4000-8000-000000000002"},
            site="LEGACY-SITE",
        )
    )

    assert calls[0][-1] == ["LEGACY-SITE"]
    assert len(calls) == 1
    assert result["site"] == "LEGACY-SITE"


def test_transfer_result_projects_device_root_to_resource_slot_reference() -> None:
    """设备型库位父资源也必须输出与合同一致的单 ResourceSlot UUID 对象。"""

    mount = {
        "uuid": "50000000-0000-4000-8000-000000000009",
        "type": "device",
        "class": "community.szlab_poly_studio.szlab_mixer_pipetting_station",
    }

    assert _dump_resource_slot(mount) == {
        "uuid": "50000000-0000-4000-8000-000000000009"
    }


def test_transfer_result_rejects_resource_without_stable_uuid() -> None:
    """单 ResourceSlot 缺少稳定 UUID 时失败关闭，不回退为资源树数组。"""

    with pytest.raises(ValueError, match="缺少稳定 UUID"):
        _dump_resource_slot({"name": "unstable-resource"})


def test_host_discard_returns_runtime_receipt_without_inventory_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime 废弃动作只能移除设备资源树并回传回执，不能写 Scheduler 库存。"""

    def _forbid_inventory_access() -> Any:
        raise AssertionError("Edge Runtime must not access scheduler inventory")

    monkeypatch.setattr(
        "unilabos.app.scheduler.integration.get_inventory_service",
        _forbid_inventory_access,
    )
    monkeypatch.setattr(
        "unilabos.ros.nodes.presets.host_node.ResourceTreeSet.from_plr_resources",
        lambda _resources: SimpleNamespace(
            dump=lambda: [[{"barcode": "BC-1"}]]
        ),
    )
    runtime = SimpleNamespace(
        lab_logger=lambda: logging.getLogger(__name__),
        notify_resource_tree_update=lambda edge, action, uuids: (
            edge,
            action,
            uuids,
        )
        == ("device-1", "remove", ["material-1"]),
    )

    result = asyncio.run(
        HostNode.discard_resource(
            runtime,
            SimpleNamespace(unilabos_uuid="material-1", name="sample"),
            "device-1",
        )
    )

    assert result == {
        "code": 0,
        "uuids": ["material-1"],
        "device_id": "device-1",
        "status": "runtime_removed",
    }
