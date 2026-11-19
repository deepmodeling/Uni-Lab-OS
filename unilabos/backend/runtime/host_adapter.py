"""Host 执行适配器的 transport 中立基类。

:class:`unilabos.backend.host_services.HostServices` 定义 host_node 的服务动作，
ROS2 :class:`unilabos.backend.ros2.presets.host_node.HostNode` 与 HostLink
:class:`unilabos.backend.hostlink.host_node.HostNode` 分别负责各自 transport
的执行编排。本模块提供两种 transport 共用的设备状态、goal 簿记、桥接器
通知、延迟诊断和 test_mode 行为。微后端通过 ``adapter_registry``
只依赖该公共契约。
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

from unilabos.utils import logger

if TYPE_CHECKING:
    from unilabos.server.backend.execution_queue import QueueItem

#: 与 rclpy ``GoalStatus`` 对齐的数值（基类不依赖 ROS 运行时）。
GOAL_STATUS_UNKNOWN = 0
GOAL_STATUS_EXECUTING = 2


def execution_result_bridges(bridges: Iterable[Any]) -> list[Any]:
    """存在微后端生命周期 owner 时，只把原始执行结果交给该 owner。"""

    values = list(bridges)
    owners = [
        bridge
        for bridge in values
        if bool(getattr(bridge, "owns_job_lifecycle", False))
    ]
    return owners or values


class HostAdapterBase:
    """两种 backend 执行适配器的公共骨架。

    子类必须实现 transport 特定的两个能力：``send_goal`` / ``cancel_job``。
    物料/设备管理下行不在本契约内——它们是模块级函数
    （:mod:`unilabos.backend.hostlink.downlink`），微后端直接调用。
    """

    #: 设备命名空间前缀（ROS2 侧按设备实例覆写为 ``/devices/<id>``）。
    namespace: str = "/devices"
    #: 单次应用层 ping-pong 等待上限；使用 monotonic 时钟，避免系统校时影响超时。
    _PING_TIMEOUT_SECONDS = 10.0

    def __init__(self, bridges: Optional[List[Any]] = None) -> None:
        self.bridges: List[Any] = list(bridges or [])
        # 服务端最近一次任务下发时间戳（test_latency 用）
        self.server_latest_timestamp: float = 0.0
        # 设备面簿记（capabilities 快照与微后端调度以此为准）
        self.devices_names: Dict[str, str] = {}
        self.device_machine_names: Dict[str, str] = {}
        self._action_value_mappings: Dict[str, Dict[str, Any]] = {}
        self._online_devices: Set[str] = set()
        # 设备状态簿记
        self.device_status: Dict[str, Dict[str, Any]] = {}
        self.device_status_timestamps: Dict[str, Dict[str, float]] = {}
        self._subscribed_topics: Set[str] = set()
        # goal / job 状态簿记
        self._goals: Dict[str, Any] = {}
        self._inflight_goal_jobs: Set[str] = set()
        self._canceled_jobs: Set[str] = set()
        self._goal_state_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 日志接口
    # ------------------------------------------------------------------

    def lab_logger(self):
        return logger

    # ------------------------------------------------------------------
    # transport 契约（子类实现）
    # ------------------------------------------------------------------

    def send_goal(
        self,
        item: "QueueItem",
        action_type: str,
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, Any],
        server_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    def cancel_job(self, job_id: str) -> bool:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 共享默认实现
    # ------------------------------------------------------------------

    def cancel_goal(self, goal_uuid: str) -> bool:
        """使用 goal 命名调用 ``cancel_job``。"""

        return self.cancel_job(goal_uuid)

    def get_goal_status(self, job_id: str) -> int:
        """未持有 goal 句柄的默认口径：in-flight 视为执行中。"""

        if job_id in self._inflight_goal_jobs:
            return GOAL_STATUS_EXECUTING
        return GOAL_STATUS_UNKNOWN

    # ------------------------------------------------------------------
    # 桥接器通知（两种 backend 共用）
    # ------------------------------------------------------------------

    def notify_ready(self) -> None:
        """告知所有桥接器 Host 可用。"""
        for bridge in self.bridges:
            method_names = (
                ("publish_host_ready",)
                if callable(getattr(bridge, "publish_host_ready", None))
                else (
                    "report_action_error_decisions",
                    "report_all_action_locks",
                )
            )
            for method_name in method_names:
                method = getattr(bridge, method_name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:  # noqa: BLE001 - bridge may be offline
                        logger.exception("[Host Node] bridge %s 初始化通知失败", method_name)

    def _notify_capabilities_changed(self) -> None:
        """设备/动作能力集变化：通知桥接器刷新 runtime.v1 endpoint 能力快照。"""
        for bridge in self.bridges:
            callback = getattr(bridge, "publish_capabilities_changed", None)
            if callable(callback):
                try:
                    callback()
                except Exception:  # noqa: BLE001 - 能力快照失败不影响设备发现
                    self.lab_logger().debug("capabilities 快照通知失败", exc_info=True)

    def _report_action_locks_free(self, action_pairs: List[Tuple[str, str]]) -> None:
        """向所有桥接器主动上报新发现 action 的锁状态为 free。

        _execute_driver_command[_async] 是通用驱动命令入口，并非具体业务动作，
        不作为锁上报（与 WebSocketClient.report_all_action_locks 的过滤保持一致）。
        """
        if not action_pairs:
            return
        locks = [
            {"device_id": dev, "action_name": act, "free": True}
            for dev, act in action_pairs
            if not act.startswith("_execute_driver_command")
        ]
        if not locks:
            return
        for bridge in self.bridges:
            if hasattr(bridge, "publish_action_locks"):
                try:
                    bridge.publish_action_locks(locks)
                except Exception as e:
                    self.lab_logger().warning(f"[Host Node] publish_action_locks failed: {e}")

    def _publish_terminal_result(
        self,
        item: "QueueItem",
        status: str,
        return_info: Dict[str, Any],
        result_data: Dict[str, Any],
    ) -> None:
        """清理 goal 簿记，并把原始终态交给微后端（两种 backend 共用）。"""
        self._goals.pop(item.job_id, None)
        self._inflight_goal_jobs.discard(item.job_id)
        with self._goal_state_lock:
            self._canceled_jobs.discard(item.job_id)
        for bridge in execution_result_bridges(self.bridges):
            publish_status = getattr(bridge, "publish_job_status", None)
            if callable(publish_status):
                publish_status(result_data, item, status, return_info)

    def handle_pong_response(self, pong_data: Dict[str, Any]) -> None:
        """兼容入口：pong 的簿记在 Backend 会话上（BaseBackendClient.handle_pong）。"""

        from unilabos.server.backend.legacy_adaptor.session import get_backend_client

        if not get_backend_client().handle_pong(pong_data):
            self.lab_logger().debug("忽略没有等待者或无效的 Pong 响应")

    # ── 链路延迟诊断（test_latency） ─────────────────────────────────

    #: 每条链路的 ping-pong 轮数
    _LATENCY_ROUNDS = 5

    @staticmethod
    def _latency_sample(send_timestamp: float, receive_timestamp: float, server_timestamp: float) -> Dict[str, float]:
        rtt_ms = (receive_timestamp - send_timestamp) * 1000
        # 客户端与服务端时间差：假设网络延迟对称，取中间点的服务端时间
        mid_point_time = send_timestamp + (receive_timestamp - send_timestamp) / 2
        return {"rtt_ms": rtt_ms, "time_diff_ms": (mid_point_time - server_timestamp) * 1000}

    @staticmethod
    def _summarize_link(samples: List[Dict[str, float]]) -> Dict[str, Any]:
        if not samples:
            return {"avg_rtt_ms": -1.0, "avg_time_diff_ms": -1.0, "max_time_error_ms": -1.0, "test_count": 0}
        rtts = [s["rtt_ms"] for s in samples]
        diffs = [s["time_diff_ms"] for s in samples]
        return {
            "avg_rtt_ms": sum(rtts) / len(rtts),
            "avg_time_diff_ms": sum(diffs) / len(diffs),
            "max_time_error_ms": max(abs(min(diffs)), abs(max(diffs))),
            "test_count": len(samples),
        }

    def _probe_link(self, link: Any) -> Dict[str, Any]:
        """对会话描述的一条链路做 N 轮 ping-pong；不可用的链路直接带原因返回。"""

        log = self.lab_logger()
        result: Dict[str, Any] = {**link.describe(), **self._summarize_link([])}
        if not link.available or link.ping is None:
            result["status"] = link.reason or "unavailable"
            log.info(f"{link.name}（{link.transport}）不可用：{result['status']}，跳过")
            return result
        samples: List[Dict[str, float]] = []
        for i in range(self._LATENCY_ROUNDS):
            send_timestamp = time.time()
            server_timestamp = link.ping(self._PING_TIMEOUT_SECONDS)
            if server_timestamp is None:
                log.error(f"❌ {link.name} 第{i + 1}次 ping 失败")
                continue
            sample = self._latency_sample(send_timestamp, time.time(), float(server_timestamp))
            samples.append(sample)
            log.info(f"✅ {link.name} 第{i + 1}次: 往返={sample['rtt_ms']:.2f}ms, 时间差={sample['time_diff_ms']:.2f}ms")
        result.update(self._summarize_link(samples))
        result["status"] = "success" if samples else "all_timeout"
        return result

    def test_latency(self) -> Dict[str, Any]:
        """Edge ↔ Backend 链路延迟诊断：对会话描述的每条链路做 ping-pong。

        拓扑由 Backend 会话抽象（``describe_links``）：同一 Backend 地址（``--address``，缺省本机
        Backend 端口）上的 HTTP 数据面 + runtime.v1 控制 WebSocket。顶层统计取 HTTP 数据面（Edge
        所有数据请求都走它），``links`` 给每条链路的目标、传输、状态与统计；任务下发延迟用测出的
        时钟偏差校正。
        """

        from unilabos.server.backend.legacy_adaptor.session import get_backend_client

        log = self.lab_logger()
        client = get_backend_client()
        address_source = client.address_source()
        base_url = client.backend_url()
        log.info("=" * 60)
        log.info(
            f"开始 Edge ↔ Backend 链路延迟测试（Backend {base_url}，"
            f"{'--address 指定' if address_source == 'configured' else '缺省本机 Backend 端口'}）..."
        )
        task_start_time = time.time()

        links: Dict[str, Dict[str, Any]] = {link.name: self._probe_link(link) for link in client.describe_links()}
        http_link = links.get("http") or {**self._summarize_link([]), "status": "unavailable"}
        measured = [link for link in links.values() if link["test_count"]]
        primary = http_link if http_link["test_count"] else (measured[0] if measured else http_link)
        if measured:
            status = "success"
        elif all(link["status"] in {"not_connected", "unavailable", "unreachable"} for link in links.values()):
            status = "backend_unreachable"
        else:
            status = "all_timeout"

        log.info("-" * 50)
        log.info("[测试统计]")
        for link in links.values():
            if link["test_count"]:
                log.info(
                    f"{link['name']}（{link['transport']}）{link['target']}: {link['test_count']}/{self._LATENCY_ROUNDS} 次有效，"
                    f"平均往返 {link['avg_rtt_ms']:.2f}ms，平均时间差 {link['avg_time_diff_ms']:.2f}ms，"
                    f"最大时间误差 ±{link['max_time_error_ms']:.2f}ms"
                )
            else:
                log.info(f"{link['name']}（{link['transport']}）{link['target'] or '-'}: {link['status']}")

        raw_delay_ms = -1.0
        corrected_delay_ms = -1.0
        if primary["test_count"] and self.server_latest_timestamp > 0:
            log.info("-" * 50)
            log.info("[任务执行延迟分析]")
            raw_delay_ms = (task_start_time - self.server_latest_timestamp) * 1000
            corrected_delay_ms = raw_delay_ms - primary["avg_time_diff_ms"]
            log.info(f"📊 原始时间差: {raw_delay_ms:.2f}ms")
            log.info(f"🔧 时间同步校正: {primary['avg_time_diff_ms']:.2f}ms")
            log.info(f"⏰ 实际任务延迟: {corrected_delay_ms:.2f}ms（误差 ±{primary['max_time_error_ms']:.2f}ms）")
        elif primary["test_count"]:
            log.warning("⚠️ 无法获取服务端任务下发时间，跳过任务延迟分析")
        else:
            log.error("❌ 所有链路的 ping-pong 都失败了")
        log.info("=" * 60)

        return {
            "avg_rtt_ms": primary["avg_rtt_ms"],
            "avg_time_diff_ms": primary["avg_time_diff_ms"],
            "max_time_error_ms": primary["max_time_error_ms"],
            "task_delay_ms": corrected_delay_ms if corrected_delay_ms > 0 else -1.0,
            "raw_delay_ms": raw_delay_ms if (primary["test_count"] and self.server_latest_timestamp > 0) else -1.0,
            "test_count": primary["test_count"],
            "status": status,
            "backend_url": base_url,
            "address_source": address_source,
            "links": links,
        }

    def _build_test_mode_return(
        self, device_id: str, action_name: str, action_kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按注册表 handles 的 output 定义构建 test_mode 模拟返回值（规则见 ``runtime.test_mode``）。"""

        from unilabos.backend.runtime.test_mode import build_test_mode_return

        del action_kwargs
        action_mapping = self._action_value_mappings.get(device_id, {}).get(action_name, {})
        return build_test_mode_return(action_name, action_mapping)


__all__ = [
    "GOAL_STATUS_EXECUTING",
    "GOAL_STATUS_UNKNOWN",
    "HostAdapterBase",
    "execution_result_bridges",
]
