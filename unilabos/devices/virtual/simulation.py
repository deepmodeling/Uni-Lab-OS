"""内置虚拟设备共用的模拟工具。

- **模拟时间**：动作按「模拟时长 × 时间倍率」真实等待。倍率只按设备配：图节点配置 ``time_scale``，
  没配时取设备类自带的默认值 ``default_time_scale``（0.001：一小时的模拟操作约 3.6 秒跑完）；
  1.0 按真实时长，0 不等待。
- **长动作**：分步推进，每步检查取消（``ActionContext``，ROS 2 不注入时只是不支持取消）、
  更新进度与剩余时间，并通过动作反馈发布。
- **容器引用**：协议编译器按 ``{"id": <设备图节点 id>}`` 传容器，前端 / 脚本也可能传 uuid、
  字符串，ROS 2 原生动作里 ``Resource`` 字段到达时已是 PLR 实例。统一解析成本地物料实例
  （内容物读写走 PLR 体积跟踪器，提交后由快照观察者上报物料权威）与人读标签（``display_name``）。
  容器不在本设备可见范围内（Slave 进程、没挂在图上的名字）时只记账失败，不让动作失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from unilabos.backend.runtime.action import ActionCancelled, ActionContext
from unilabos.registry.decorators import not_action

#: 容器引用的线上形态：图节点 id / uuid 字符串，或 ``{"id": ..., "uuid": ...}``。
VesselRef = Union[str, Dict[str, Any]]

#: 设备类自带的默认时间倍率：一小时的模拟操作约 3.6 秒跑完。
DEFAULT_TIME_SCALE = 0.001
#: 每步最长真实等待（秒）与最多步数：决定进度反馈的粒度。
STEP_WALL_SECONDS = 0.1
MAX_STEPS = 200
#: 一步里两次取消检查之间的最长真实等待（秒）：按真实时长跑长动作时取消也及时生效。
CANCEL_CHECK_SECONDS = 0.25
#: 体积跟踪器的体积单位是 µL；设备参数（泵、过滤体积）按 mL。
UL_PER_ML = 1000.0
UG_PER_G = 1_000_000.0

_DURATION_UNITS = {
    "": 1.0, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
}
_MASS_UNITS = {
    "": 1.0, "g": 1.0, "gram": 1.0, "grams": 1.0,
    "mg": 1e-3, "milligram": 1e-3, "milligrams": 1e-3,
    "kg": 1e3, "kilogram": 1e3, "kilograms": 1e3,
    "ug": 1e-6, "µg": 1e-6, "μg": 1e-6, "microgram": 1e-6, "micrograms": 1e-6,
}
_AMOUNT_UNITS = {"mol": 1.0, "mmol": 1e-3, "umol": 1e-6, "µmol": 1e-6, "μmol": 1e-6}
_QUANTITY = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([a-zA-Zµμ]*)\s*$")


def resolve_time_scale(value: Any = None, default: float = DEFAULT_TIME_SCALE) -> float:
    """时间倍率：设备图节点配置的 ``time_scale``，没配时取设备类的默认值。"""

    raw = default if value is None or value == "" else value
    try:
        scale = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"time_scale 必须是数字，收到 {raw!r}") from exc
    if not math.isfinite(scale) or scale < 0:
        raise ValueError(f"time_scale 必须是不小于 0 的有限数，收到 {raw!r}")
    return scale


def _parse_quantity(value: Any, units: Mapping[str, float], what: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{what}不能是布尔值：{value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    match = _QUANTITY.match(text)
    if match is None or match.group(2).lower() not in units:
        raise ValueError(f"无法解析{what}：{value!r}（支持的单位：{', '.join(sorted(u for u in units if u))}）")
    return float(match.group(1)) * units[match.group(2).lower()]


def parse_duration(value: Any) -> float:
    """时长（秒）：数字按秒，字符串可带 s / min / h / d 单位，空值为 0。"""

    return _parse_quantity(value, _DURATION_UNITS, "时长")


def parse_mass(value: Any) -> float:
    """质量（g）：数字按 g，字符串可带 g / mg / kg / ug 单位，空值为 0。"""

    return _parse_quantity(value, _MASS_UNITS, "质量")


def parse_amount(value: Any) -> float:
    """物质的量（mol）：字符串须带 mol / mmol / umol 单位，空值为 0。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return _parse_quantity(value, _AMOUNT_UNITS, "物质的量")


def nested_settings(kwargs: Dict[str, Any], **explicit: Any) -> Dict[str, Any]:
    """构造参数：图节点平铺写的参数为准；写成 ``{"config": {...}}``（旧注册表
    ``init_param_schema`` 的形状，编辑器按它生成）时内层的同名键覆盖。"""

    nested = kwargs.pop("config", None)
    if isinstance(nested, dict):
        for key in explicit:
            if key in nested:
                explicit[key] = nested[key]
    return explicit


def is_empty_ref(ref: Any) -> bool:
    """容器引用为空（None、空串、空字典）——表示「未指定」。"""

    if ref is None:
        return True
    if isinstance(ref, str):
        return not ref.strip()
    if isinstance(ref, Mapping):
        return not ref
    return False


def vessel_identity(ref: Any) -> Tuple[str, str]:
    """容器引用的 (图节点 id, uuid)，缺的一项为空串。"""

    if ref is None:
        return "", ""
    if isinstance(ref, Mapping):
        if not any(key in ref for key in ("id", "uuid", "name")) and len(ref) == 1:
            # {"<名字>": {...}} 形态（旧协议参数）
            inner = next(iter(ref.values()))
            if isinstance(inner, Mapping):
                return vessel_identity(inner)
        node_uuid = str(ref.get("uuid") or (ref.get("data") or {}).get("unilabos_uuid") or "")
        if isinstance(ref.get("id"), Mapping):
            # 控温协议把容器参数再包一层：{"id": {"id": "flask"}}
            inner_id, inner_uuid = vessel_identity(ref["id"])
            return inner_id, inner_uuid or node_uuid
        node_id = str(ref.get("id") or "")
        if not node_id and not node_uuid:
            node_id = str(ref.get("name") or "")
        return node_id, node_uuid
    resource_uuid = getattr(ref, "unilabos_uuid", None)
    if resource_uuid is not None or hasattr(ref, "get_absolute_location"):
        return str(getattr(ref, "name", "") or ""), str(resource_uuid or "")
    return str(ref).strip(), ""


def physical_graph() -> Any:
    """协议编译器用的物理拓扑图（Host 进程装配了设备图时才有，否则 None）。"""

    from unilabos.backend.runtime.workstation_protocol import physical_setup_graph

    return physical_setup_graph()


def _graph_node(node_id: str, node_uuid: str) -> Tuple[str, Dict[str, Any]]:
    """在协议编译器用的物理拓扑图里找容器节点（Host 进程装配了设备图时才有）。"""

    graph = physical_graph()
    if graph is None:
        return "", {}
    if node_id and node_id in graph.nodes:
        return node_id, dict(graph.nodes[node_id])
    if node_uuid:
        for candidate, data in graph.nodes(data=True):
            if data.get("uuid") == node_uuid:
                return candidate, dict(data)
    return "", {}


class VirtualDevice:
    """内置虚拟设备的公共实现（不是设备，本身不注册）。

    子类自己声明 ``@device`` / ``@action`` 与状态属性；这里只放模拟时间、长动作推进、
    取消与容器解析这些共用逻辑，方法一律私有，不会被注册表当成动作。
    """

    # 虚拟设备本身就是模拟器：--test_mode 下同样真实执行（只是快）
    run_in_test_mode = True
    #: 图节点没配 time_scale 时的时间倍率（设备类自带，子类可改）
    default_time_scale: float = DEFAULT_TIME_SCALE

    def __init__(self, device_id: Optional[str], default_id: str, time_scale: Any = None) -> None:
        self.device_id = str(device_id or default_id)
        self.time_scale = resolve_time_scale(time_scale, self.default_time_scale)
        self.logger = logging.getLogger(f"unilabos.devices.virtual.{self.device_id}")
        self._node: Any = None
        self._lock = threading.RLock()
        self._status = "Idle"
        self._message = ""
        self._progress = 0.0
        self._remaining_time = 0.0
        # 正在执行的长动作的上下文：stop / 复位类动作据此请求取消
        self._active_context: Optional[ActionContext] = None
        # 动作嵌套深度：transfer 里的每次柱塞移动只更新阶段状态，收尾交给最外层
        self._operation_depth = 0

    @not_action
    def post_init(self, node: Any) -> None:
        """保存运行节点（HostLink 与 ROS 2 都是通用 DeviceNode）。"""

        self._node = node

    # ------------------------------------------------------------------
    # 模拟时间与长动作
    # ------------------------------------------------------------------

    def _plan_steps(self, simulated_seconds: float) -> Tuple[float, int]:
        wall = max(0.0, float(simulated_seconds)) * self.time_scale
        if wall <= 0:
            return 0.0, 1
        return wall, max(1, min(MAX_STEPS, math.ceil(wall / STEP_WALL_SECONDS)))

    @staticmethod
    def _check_cancel(action_context: Optional[ActionContext]) -> None:
        if action_context is not None:
            action_context.raise_if_cancelled()

    def _advance(
        self,
        fraction: float,
        simulated_seconds: float,
        progress_span: Tuple[float, float],
        remaining_after: float,
        action_context: Optional[ActionContext],
        feedback: Optional[Mapping[str, Any]],
    ) -> None:
        start, end = progress_span
        with self._lock:
            self._progress = round(start + (end - start) * fraction, 3)
            self._remaining_time = round(simulated_seconds * (1.0 - fraction) + remaining_after, 3)
        if action_context is not None:
            action_context.publish_feedback(
                {
                    "status": self._status,
                    "progress": self._progress,
                    "remaining_time": self._remaining_time,
                    **dict(feedback or {}),
                }
            )

    async def _sleep(self, seconds: float) -> None:
        # ROS 2 节点的 sleep 走 rclpy 的定时器；没有节点（直接实例化）时用 asyncio
        if self._node is not None and hasattr(self._node, "sleep"):
            await self._node.sleep(seconds)
        else:
            await asyncio.sleep(seconds)

    async def _simulate(
        self,
        simulated_seconds: float,
        action_context: Optional[ActionContext],
        *,
        progress_span: Tuple[float, float] = (0.0, 100.0),
        remaining_after: float = 0.0,
        feedback: Optional[Mapping[str, Any]] = None,
        on_step: Any = None,
    ) -> None:
        """按模拟时长推进（异步动作用）：每步检查取消、更新进度 / 剩余时间并发布反馈。

        ``on_step(fraction)`` 在每步之后调用，用来演化温度、体积等状态。
        """

        total = max(0.0, float(simulated_seconds))
        wall, steps = self._plan_steps(total)
        for index in range(1, steps + 1):
            self._check_cancel(action_context)
            pending = wall / steps
            while pending > 0:
                chunk = min(pending, CANCEL_CHECK_SECONDS)
                await self._sleep(chunk)
                pending -= chunk
                if pending > 0:
                    self._check_cancel(action_context)
            fraction = index / steps
            if on_step is not None:
                on_step(fraction)
            self._advance(fraction, total, progress_span, remaining_after, action_context, feedback)
        self._check_cancel(action_context)

    def _simulate_blocking(
        self,
        simulated_seconds: float,
        action_context: Optional[ActionContext],
        *,
        progress_span: Tuple[float, float] = (0.0, 100.0),
        feedback: Optional[Mapping[str, Any]] = None,
        on_step: Any = None,
    ) -> None:
        """同 ``_simulate``，给同步动作用（运行时把同步动作放在线程里执行）。"""

        total = max(0.0, float(simulated_seconds))
        wall, steps = self._plan_steps(total)
        for index in range(1, steps + 1):
            self._check_cancel(action_context)
            pending = wall / steps
            while pending > 0:
                chunk = min(pending, CANCEL_CHECK_SECONDS)
                time.sleep(chunk)
                pending -= chunk
                if pending > 0:
                    self._check_cancel(action_context)
            fraction = index / steps
            if on_step is not None:
                on_step(fraction)
            self._advance(fraction, total, progress_span, 0.0, action_context, feedback)
        self._check_cancel(action_context)

    @contextlib.contextmanager
    def _operation(
        self,
        status: str,
        message: str,
        action_context: Optional[ActionContext] = None,
        *,
        idle_status: str = "Idle",
    ) -> Iterator[None]:
        """一次动作的状态收尾：开始置忙，成功回到空闲，取消回到空闲并记一句，失败置 Error。

        动作期间记下它的 ``action_context``，停止类动作（``_request_stop``）据此请求取消。
        嵌套调用（外层动作里复用的子步骤）只更新状态与说明，收尾交给最外层。
        """

        with self._lock:
            nested = self._operation_depth > 0
            self._operation_depth += 1
            self._status = status
            self._message = message
            if not nested:
                self._progress = 0.0
                self._active_context = action_context
        if nested:
            try:
                yield
            finally:
                with self._lock:
                    self._operation_depth -= 1
            return
        try:
            yield
        except (ActionCancelled, asyncio.CancelledError):
            with self._lock:
                self._status = idle_status
                self._message = f"已取消：{message}"
                self._remaining_time = 0.0
            self.logger.info("[%s] 动作已取消：%s", self.device_id, message)
            raise
        except Exception as exc:
            with self._lock:
                self._status = "Error"
                self._message = str(exc)
                self._remaining_time = 0.0
            raise
        else:
            with self._lock:
                self._status = idle_status
                self._progress = 100.0
                self._remaining_time = 0.0
        finally:
            with self._lock:
                self._operation_depth -= 1
                self._active_context = None

    def _request_stop(self) -> bool:
        """请求取消正在执行的长动作；没有在跑的动作时返回 False。"""

        with self._lock:
            context = self._active_context
        if context is None:
            return False
        context.request_cancel()
        return True

    def _reject(self, message: str) -> None:
        """参数不合法：记下原因并抛 ValueError（作业进入失败决策，不当成功返回）。"""

        with self._lock:
            self._message = message
        raise ValueError(message)

    # ------------------------------------------------------------------
    # 容器：标签与内容物
    # ------------------------------------------------------------------

    def _vessel_label(self, ref: Any) -> str:
        """容器的人读标签：引用自带的 display_name → 设备图里的 display_name → 图节点 id / uuid。"""

        if is_empty_ref(ref):
            return ""
        if isinstance(ref, Mapping) and ref.get("display_name"):
            return str(ref["display_name"])
        node_id, node_uuid = vessel_identity(ref)
        resolved_id, data = _graph_node(node_id, node_uuid)
        return str(data.get("display_name") or resolved_id or node_id or node_uuid)

    def _vessel_resource(self, ref: Any) -> Any:
        """本设备可见的 PLR 物料实例（找不到返回 None）。"""

        if is_empty_ref(ref):
            return None
        if hasattr(ref, "unilabos_uuid") and hasattr(ref, "get_absolute_location"):
            return ref
        tracker = getattr(self._node, "resource_tracker", None)
        if tracker is None:
            return None
        node_id, node_uuid = vessel_identity(ref)
        if not node_uuid:
            _resolved, data = _graph_node(node_id, "")
            node_uuid = str(data.get("uuid") or "")
        if node_uuid:
            found = tracker.uuid_to_resources.get(node_uuid)
            if found is not None:
                return found
            matches = tracker.figure_resource({"uuid": node_uuid}, try_mode=True)
            if len(matches) == 1:
                return matches[0]
        return None

    def _vessel_tracker(self, ref: Any) -> Any:
        resource = self._vessel_resource(ref)
        tracker = getattr(resource, "tracker", None)
        if tracker is None or getattr(tracker, "is_disabled", False):
            if not is_empty_ref(ref):
                self.logger.debug("[%s] 容器 %s 不可见或没有体积跟踪器，不记内容物", self.device_id, ref)
            return None
        return tracker

    @staticmethod
    def _commit(tracker: Any) -> None:
        # 提交后快照观察者把内容物上报物料权威
        tracker.commit()

    def _vessel_contents(self, ref: Any) -> List[Dict[str, Any]]:
        """容器当前内容物（液体与固体）；不可见时为空列表。"""

        tracker = self._vessel_tracker(ref)
        if tracker is None:
            return []
        entries = list(tracker.get_liquids()) + list(tracker.get_solids())
        return [{"name": str(name or ""), "amount": round(float(amount), 6), "unit": str(unit)} for name, amount, unit in entries]

    def _add_to_vessel(self, ref: Any, name: str, amount: float, unit: str) -> float:
        """向容器加入内容物（液体按剩余容积截断，固体不占容积）；返回实际加入量。"""

        tracker = self._vessel_tracker(ref)
        if tracker is None or amount <= 0:
            return 0.0
        if unit == "ul":
            amount = min(float(amount), max(0.0, float(tracker.get_free_volume())))
            if amount <= 0:
                return 0.0
        tracker.add_liquid(name or None, float(amount), unit=unit)
        self._commit(tracker)
        return float(amount)

    def _transfer_liquid(self, source: Any, target: Any, volume_ul: float) -> List[Tuple[str, float, str]]:
        """从 ``source`` 按比例取液体放进 ``target``；返回转移的 (名称, 体积 µL, 单位)。

        转移量不超过源容器现有量，目标容器可见时也不超过它的剩余容积（放不下的留在源容器）；
        目标未指定或不可见时取出的液体离开记账范围（如滤液、废液）。源容器不可见时不记账。
        """

        source_tracker = self._vessel_tracker(source)
        if source_tracker is None or volume_ul <= 0:
            return []
        target_tracker = None if is_empty_ref(target) else self._vessel_tracker(target)
        if target_tracker is source_tracker:
            return []
        volume = min(float(volume_ul), float(source_tracker.get_used_volume()))
        if target_tracker is not None:
            volume = min(volume, max(0.0, float(target_tracker.get_free_volume())))
        if volume <= 0:
            return []
        removed = [(str(name or ""), float(amount), str(unit)) for name, amount, unit in source_tracker.remove_liquid(volume)]
        self._commit(source_tracker)
        if target_tracker is not None and removed:
            for name, amount, unit in removed:
                target_tracker.add_liquid(name or None, amount, unit=unit)
            self._commit(target_tracker)
        return removed

    def _drop_from_vessel(self, ref: Any, names: List[str]) -> float:
        """从容器里去掉指定名称的液体（蒸发溶剂）；``names`` 为空时去掉全部液体。返回去掉的体积。"""

        tracker = self._vessel_tracker(ref)
        if tracker is None:
            return 0.0
        wanted = {name.strip().lower() for name in names if name and name.strip()}
        liquids = list(tracker.get_liquids())
        kept = [item for item in liquids if wanted and str(item[0] or "").lower() not in wanted]
        dropped = sum(float(amount) for _name, amount, _unit in liquids) - sum(float(item[1]) for item in kept)
        if dropped <= 0:
            return 0.0
        tracker.set_liquids([*kept, *tracker.get_solids()])
        self._commit(tracker)
        return dropped


__all__ = [
    "CANCEL_CHECK_SECONDS",
    "DEFAULT_TIME_SCALE",
    "MAX_STEPS",
    "STEP_WALL_SECONDS",
    "UG_PER_G",
    "UL_PER_ML",
    "VesselRef",
    "VirtualDevice",
    "is_empty_ref",
    "nested_settings",
    "parse_amount",
    "parse_duration",
    "parse_mass",
    "physical_graph",
    "resolve_time_scale",
    "vessel_identity",
]
