"""测试动作设备。

用于验证前端参数填写、ResourceSlot/DeviceSlot 传递、动作返回值和 workflow handle 输出链路。
"""

import logging
import time
import types
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from typing_extensions import TypedDict

from unilabos.registry.decorators import (
    ActionInputHandle,
    ActionOutputHandle,
    DataSource,
    action,
    device,
    not_action,
    topic_config,
)
from unilabos.registry.placeholder_type import DeviceSlot, ResourceSlot
from unilabos.resources.resource_tracker import ResourceDict, ResourceTreeSet
if TYPE_CHECKING:
    from unilabos.device_runtime.node import DeviceNode


class ResourceSummary(TypedDict):
    """回显资源的人类可读摘要。"""

    index: int
    name: str
    class_name: str
    liquids: List[Dict[str, Any]]
    total_volume: float
    repr: str


class TestReportItem(TypedDict):
    """单个测试检查项。"""

    name: str
    passed: bool
    message: str


class TestResourceActionReturn(TypedDict):
    """测试资源动作返回值。"""

    success: bool
    message: str
    run_id: str
    test_name: str
    resource_count: int
    expected_resource_count: int
    resource_tree: List[List[ResourceDict]]
    resource_summary: List[ResourceSummary]
    checks: Dict[str, bool]
    test_report: List[TestReportItem]
    total_volume: float
    elapsed_seconds: float


@device(
    id="test_action",
    displayname="测试动作设备",
    category=["virtual"],
    description="用于测试动作调用、资源参数、设备参数、返回值和 workflow handle 链路的虚拟设备",
)
class TestActionDevice:
    """集中放置测试用动作的虚拟设备。"""

    _ros_node: "DeviceNode"

    def __init__(self, device_id: Optional[str] = None, **kwargs):
        """
        初始化测试动作设备。

        Args:
            device_id[设备ID]: 设备实例 ID，默认使用 test_action。
        """
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")

        self.device_id = device_id or "test_action"
        self.logger = logging.getLogger(f"TestActionDevice.{self.device_id}")
        self.data: Dict[str, Any] = {
            "status": "Idle",
            "last_run_id": "",
            "last_message": "",
            "last_resource_count": 0,
            "last_device_count": 0,
        }

    @not_action
    def post_init(self, ros_node: "DeviceNode") -> None:
        """保存运行节点引用，供后续扩展跨设备调用使用。"""
        self._ros_node = ros_node

    @property
    @topic_config(period=2.0)
    def status(self) -> str:
        """设备当前状态。"""
        return self.data.get("status", "Idle")

    def _get_resource_state(self, resource: ResourceSlot) -> Dict[str, Any]:
        """读取资源 state，失败时返回空字典。"""
        try:
            state = resource.serialize_state()
            return dict(state) if isinstance(state, dict) else {}
        except Exception as exc:
            self.logger.warning(f"读取资源状态失败: {resource!r}, error={exc}")
            return {}

    def _set_resource_state(self, resource: ResourceSlot, state: Dict[str, Any]) -> None:
        """在当前资源实例上覆盖 serialize_state，用于测试动作输出更新后的资源树。"""
        resource.unilabos_test_state = dict(state)

        def serialize_state(current_resource):
            return dict(getattr(current_resource, "unilabos_test_state", {}))

        resource.serialize_state = types.MethodType(serialize_state, resource)

    def _get_liquids(self, resource: ResourceSlot) -> List[Dict[str, Any]]:
        """从资源 state 中提取液体列表，兼容 liquid 和 liquid_type/liquid_volume 两种格式。"""
        state = self._get_resource_state(resource)
        liquids = state.get("liquid")
        if isinstance(liquids, list):
            return [item for item in liquids if isinstance(item, dict)]

        liquid_types = state.get("liquid_type", [])
        liquid_volumes = state.get("liquid_volume", [])
        if not isinstance(liquid_types, list):
            liquid_types = [liquid_types]
        if not isinstance(liquid_volumes, list):
            liquid_volumes = [liquid_volumes]

        normalized = []
        for index, liquid_type in enumerate(liquid_types):
            volume = liquid_volumes[index] if index < len(liquid_volumes) else 0.0
            normalized.append({"name": str(liquid_type), "volume": float(volume)})
        return normalized

    def _get_total_volume(self, resource: ResourceSlot) -> float:
        """统计资源中的液体总体积。"""
        total = 0.0
        for liquid in self._get_liquids(resource):
            try:
                total += float(liquid.get("volume", 0.0))
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _normalize_devices(devices: Any) -> List[str]:
        """兼容前端传入单个 DeviceSlot 字符串或 DeviceSlot 列表。"""
        if devices is None:
            return []
        if isinstance(devices, str):
            return [devices] if devices else []
        if isinstance(devices, list):
            return [str(item) for item in devices if str(item)]
        return [str(devices)]

    def _summarize_resource(self, index: int, resource: ResourceSlot) -> ResourceSummary:
        """生成稳定、可 JSON 序列化的资源摘要。"""
        liquids = self._get_liquids(resource)
        return {
            "index": index,
            "name": str(getattr(resource, "name", "")),
            "class_name": resource.__class__.__name__,
            "liquids": liquids,
            "total_volume": round(self._get_total_volume(resource), 3),
            "repr": repr(resource),
        }

    @staticmethod
    def _report_item(name: str, passed: bool, message: str) -> TestReportItem:
        """生成一个测试报告条目。"""
        return {"name": name, "passed": passed, "message": message}

    @action(
        always_free=True,
        description="测试液体写入：向资源中追加或替换液体信息，并输出更新后的资源",
        goal_default={
            "test_name": "liquid write test",
            "liquid_name": "test_liquid",
            "volume": 10.0,
            "concentration": 1.0,
            "mode": "replace",
            "delay_seconds": 0.0,
            "fail_when_empty": False,
            "resources": [],
        },
        handles=[
            ActionInputHandle(
                key="input_resources",
                data_type="resource",
                label="输入资源",
                data_key="resources",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="output_resources",
                data_type="resource",
                label="写入后的资源",
                data_key="resource_tree.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def test_liquid(
        self,
        test_name: str = "liquid write test",
        liquid_name: str = "test_liquid",
        volume: float = 10.0,
        concentration: float = 1.0,
        mode: str = "replace",
        delay_seconds: float = 0.0,
        fail_when_empty: bool = False,
        resources: List[ResourceSlot] = None,
    ) -> TestResourceActionReturn:
        """
        向资源中写入测试液体信息，并把更新后的资源作为 handle 输出。

        Args:
            test_name[测试名称]: 普通字符串参数，会原样返回到测试报告。
            liquid_name[液体名称]: 写入资源的液体名称。
            volume[液体体积]: 写入资源的液体体积。
            concentration[液体浓度]: 写入资源的液体浓度，仅用于测试数据展示。
            mode[写入模式]: replace 表示替换原有 liquid；append 表示追加到原有 liquid。
            delay_seconds[模拟耗时(s)]: 动作内 sleep 的秒数，用于测试任务耗时和日志。
            fail_when_empty[空资源时失败]: 为 true 且没有传入资源时，动作直接失败。
            resources[资源列表]: 前端选择或上游 handle 传入的多个资源，可不填。
        """
        start_time = time.time()
        run_id = str(uuid.uuid4())[:8]

        self.data["status"] = "Running"
        self.data["last_run_id"] = run_id

        if resources is None:
            resources = []
        if delay_seconds < 0:
            raise ValueError("delay_seconds 不能小于 0")
        if volume < 0:
            raise ValueError("volume 不能小于 0")
        if mode not in {"replace", "append"}:
            raise ValueError("mode 只能是 replace 或 append")

        if delay_seconds > 0:
            time.sleep(delay_seconds)

        selected_resources = list(resources)
        resource_count = len(selected_resources)

        if fail_when_empty and resource_count == 0:
            self.data["status"] = "Error"
            self.data["last_message"] = "测试失败：未传入资源"
            raise ValueError("fail_when_empty=True，但 resources 为空")

        liquid = {"name": liquid_name, "volume": float(volume), "concentration": float(concentration)}
        for resource in selected_resources:
            state = self._get_resource_state(resource)
            current_liquids = self._get_liquids(resource)
            next_liquids = current_liquids + [liquid] if mode == "append" else [liquid]
            state["liquid"] = next_liquids
            state["liquid_type"] = [item.get("name", "") for item in next_liquids]
            state["liquid_volume"] = [float(item.get("volume", 0.0)) for item in next_liquids]
            self._set_resource_state(resource, state)

        checks = {
            "action_invoked": True,
            "resource_slot_received": resource_count > 0,
            "liquid_written": all(self._get_liquids(resource) for resource in selected_resources),
            "handle_output_ready": True,
        }

        test_report = [
            self._report_item("action_invoked", True, "动作已被执行端调用"),
            self._report_item(
                "resource_slot_received",
                checks["resource_slot_received"],
                f"收到 {resource_count} 个资源",
            ),
            self._report_item(
                "liquid_written",
                checks["liquid_written"],
                f"以 {mode} 模式写入液体 {liquid_name}, volume={volume}",
            ),
            self._report_item("handle_output_ready", True, "已生成可供下游 handle 使用的输出资源"),
        ]

        resource_tree = (
            ResourceTreeSet.from_plr_resources(selected_resources, known_newly_created=True).dump()
            if selected_resources
            else []
        )
        resource_summary = [
            self._summarize_resource(index, item) for index, item in enumerate(selected_resources, start=1)
        ]

        total_volume = round(sum(self._get_total_volume(item) for item in selected_resources), 3)
        required_checks = {"action_invoked", "liquid_written", "handle_output_ready"}
        success = all(item["passed"] for item in test_report if item["name"] in required_checks)
        elapsed_seconds = round(time.time() - start_time, 3)
        status_icon = "✅" if success else "❌"
        message = f"{status_icon} 液体写入完成：{resource_count} 个资源，total_volume={total_volume}"

        self.data.update(
            {
                "status": "Idle" if success else "Error",
                "last_message": message,
                "last_resource_count": resource_count,
                "last_device_count": 0,
            }
        )
        self.logger.info(f"[test_liquid:{run_id}] {message}, test_name={test_name}")
        for item in test_report:
            item_icon = "✅" if item["passed"] else "❌"
            self.logger.info(f"[test_liquid:{run_id}] {item_icon} {item['name']}: {item['message']}")
        for item in resource_summary:
            self.logger.info(f"[test_liquid:{run_id}] 🧪 resource={item['name']}, liquids={item['liquids']}")

        return {
            "success": success,
            "message": message,
            "run_id": run_id,
            "test_name": test_name,
            "resource_count": resource_count,
            "expected_resource_count": -1,
            "resource_tree": resource_tree,
            "resource_summary": resource_summary,
            "checks": checks,
            "test_report": test_report,
            "total_volume": total_volume,
            "elapsed_seconds": elapsed_seconds,
        }

    @action(
        always_free=True,
        description="测试资源检查：检查资源数量、液体信息和总体积，可接收上游 test_liquid 的输出资源",
        goal_default={
            "test_name": "resource inspection test",
            "delay_seconds": 0.0,
            "expected_resource_count": -1,
            "require_liquid": True,
            "min_total_volume": 0.0,
            "fail_on_check_failed": False,
            "resources": [],
            "devices": [],
        },
        handles=[
            ActionInputHandle(
                key="input_resources",
                data_type="resource",
                label="待检查资源",
                data_key="resources",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="checked_resources",
                data_type="resource",
                label="检查后的资源",
                data_key="resource_tree.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def test_check(
        self,
        test_name: str = "resource inspection test",
        delay_seconds: float = 0.0,
        expected_resource_count: int = -1,
        require_liquid: bool = True,
        min_total_volume: float = 0.0,
        fail_on_check_failed: bool = False,
        resources: List[ResourceSlot] = None,
        devices: List[DeviceSlot] = None,
    ) -> TestResourceActionReturn:
        """
        检查资源数量、液体信息和总体积，用于验证上游资源输出是否正确传入下游。

        Args:
            test_name[测试名称]: 普通字符串参数，会原样返回到测试报告。
            delay_seconds[模拟耗时(s)]: 动作内 sleep 的秒数，用于测试任务耗时和日志。
            expected_resource_count[期望资源数]: -1 表示不校验；其他值会和实际资源数比较。
            require_liquid[要求包含液体]: 为 true 时，所有资源都必须包含 liquid。
            min_total_volume[最小总体积]: 所有资源液体总体积必须大于等于该值。
            fail_on_check_failed[检查失败时动作失败]: 为 true 时，检查不通过会抛异常。
            resources[资源列表]: 前端选择或上游 handle 传入的多个资源，可不填。
            devices[设备列表]: 前端选择或上游 handle 传入的多个设备，可不填。
        """
        start_time = time.time()
        run_id = str(uuid.uuid4())[:8]

        self.data["status"] = "Running"
        self.data["last_run_id"] = run_id

        if resources is None:
            resources = []
        selected_devices = self._normalize_devices(devices)
        if delay_seconds < 0:
            raise ValueError("delay_seconds 不能小于 0")
        if min_total_volume < 0:
            raise ValueError("min_total_volume 不能小于 0")

        if delay_seconds > 0:
            time.sleep(delay_seconds)

        selected_resources = list(resources)
        resource_count = len(selected_resources)
        total_volume = round(sum(self._get_total_volume(item) for item in selected_resources), 3)
        resources_with_liquid = sum(1 for item in selected_resources if self._get_liquids(item))

        checks = {
            "action_invoked": True,
            "resource_slot_received": resource_count > 0,
            "resource_count_matched": expected_resource_count < 0 or resource_count == expected_resource_count,
            "liquid_required_matched": (not require_liquid) or resources_with_liquid == resource_count,
            "min_total_volume_matched": total_volume >= min_total_volume,
            "handle_output_ready": True,
        }

        test_report = [
            self._report_item("action_invoked", True, "动作已被执行端调用"),
            self._report_item("resource_slot_received", checks["resource_slot_received"], f"收到 {resource_count} 个资源"),
            self._report_item(
                "resource_count_matched",
                checks["resource_count_matched"],
                (
                    "未配置期望资源数"
                    if expected_resource_count < 0
                    else f"期望 {expected_resource_count} 个资源，实际 {resource_count} 个"
                ),
            ),
            self._report_item(
                "liquid_required_matched",
                checks["liquid_required_matched"],
                f"{resources_with_liquid}/{resource_count} 个资源包含 liquid",
            ),
            self._report_item(
                "min_total_volume_matched",
                checks["min_total_volume_matched"],
                f"总体积 {total_volume} >= 最小要求 {min_total_volume}",
            ),
            self._report_item("handle_output_ready", True, "已生成可供下游 handle 使用的输出资源"),
        ]

        required_checks = {
            "action_invoked",
            "resource_count_matched",
            "liquid_required_matched",
            "min_total_volume_matched",
            "handle_output_ready",
        }
        success = all(item["passed"] for item in test_report if item["name"] in required_checks)
        if fail_on_check_failed and not success:
            self.data["status"] = "Error"
            self.data["last_message"] = "资源检查失败"
            failed = [item["name"] for item in test_report if not item["passed"]]
            raise ValueError(f"资源检查失败: {failed}")

        resource_tree = (
            ResourceTreeSet.from_plr_resources(selected_resources, known_newly_created=True).dump()
            if selected_resources
            else []
        )
        resource_summary = [
            self._summarize_resource(index, item) for index, item in enumerate(selected_resources, start=1)
        ]

        elapsed_seconds = round(time.time() - start_time, 3)
        status_icon = "✅" if success else "❌"
        message = f"{status_icon} 资源检查完成：{resource_count} 个资源，total_volume={total_volume}"

        self.data.update(
            {
                "status": "Idle" if success else "Error",
                "last_message": message,
                "last_resource_count": resource_count,
                "last_device_count": len(selected_devices),
            }
        )
        self.logger.info(f"[test_check:{run_id}] {message}, test_name={test_name}")
        for item in test_report:
            item_icon = "✅" if item["passed"] else "❌"
            self.logger.info(f"[test_check:{run_id}] {item_icon} {item['name']}: {item['message']}")
        for item in resource_summary:
            self.logger.info(f"[test_check:{run_id}] 🧪 resource={item['name']}, liquids={item['liquids']}")

        return {
            "success": success,
            "message": message,
            "run_id": run_id,
            "test_name": test_name,
            "resource_count": resource_count,
            "expected_resource_count": expected_resource_count,
            "resource_tree": resource_tree,
            "resource_summary": resource_summary,
            "checks": checks,
            "test_report": test_report,
            "total_volume": total_volume,
            "elapsed_seconds": elapsed_seconds,
        }
