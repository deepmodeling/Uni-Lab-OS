"""HostLink 形态的工作站 XDL protocol 编排器。

设备节点壳（子设备、硬件代理、动态设备管理）由
:class:`unilabos.backend.hostlink.local_runtime.HostLinkDeviceNode` 原生承载；
本类只承载 protocol 编排——``local_runtime`` 在动作解析时把 protocol 名
路由到本类，子设备动作经 backend 无关的 ``call_device_action_async`` 派发。

ROS2 对应物是 :class:`unilabos.backend.ros2.presets.workstation.ROS2WorkstationNode`；
协议名/模型解析与资源展开/回写共用
:mod:`unilabos.backend.runtime.workstation_protocol`。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List

from unilabos.backend.runtime.workstation_protocol import (
    WorkstationNodeTempError,
    expand_resource_value,
    physical_setup_graph,
    protocol_model,
    setup_protocol_names,
    update_protocol_resources,
)
from unilabos.experiments.compile import action_protocol_generators
from unilabos.utils import logger

if TYPE_CHECKING:
    from unilabos.backend.runtime.node import DeviceNode


class WorkstationNode:
    """HostLink 工作站的 protocol 编排器（挂在 HostLinkDeviceNode 上）。"""

    def __init__(self, protocol_type: Any, host_device_node: "DeviceNode") -> None:
        self.protocol_names: List[str] = setup_protocol_names(protocol_type)
        self._host = host_device_node
        logger.info(
            f"[Workstation] protocol 编排就绪 (backend=hostlink, device={host_device_node.device_id}, "
            f"protocols={self.protocol_names})"
        )

    def protocol_action(self, action_name: str):
        """protocol 名 → 编排协程；非 protocol 返回 None（供动作解析路由）。"""
        if action_name not in self.protocol_names:
            return None
        model = protocol_model(action_name)
        generator = action_protocol_generators[model]

        async def run_protocol(**protocol_kwargs: Any) -> Dict[str, Any]:
            return await self._run_protocol(action_name, generator, protocol_kwargs)

        run_protocol.__name__ = action_name
        return run_protocol

    def _resource_field_names(self, protocol_name: str) -> List[str]:
        """注册表 placeholder_keys 中声明为资源的 goal 字段（backend 无关口径）。"""
        mapping = self._host.action_value_mappings.get(protocol_name, {})
        placeholder_keys = mapping.get("placeholder_keys") if isinstance(mapping, dict) else None
        if not isinstance(placeholder_keys, dict):
            return []
        return [
            str(field)
            for field, kind in placeholder_keys.items()
            if str(kind) == "unilabos_resources"
        ]

    async def _run_protocol(
        self,
        protocol_name: str,
        generator: Any,
        protocol_kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        host = self._host
        log = host.lab_logger()
        kwargs = dict(protocol_kwargs)

        resource_fields = self._resource_field_names(protocol_name)
        for field in resource_fields:
            if field not in kwargs:
                continue
            kwargs[field] = await expand_resource_value(host, kwargs[field])

        protocol_steps = list(generator(G=physical_setup_graph(), **kwargs))
        log.info(f"[Workstation] {protocol_name} 生成 {len(protocol_steps)} 步骤，开始执行")

        time_start = time.time()
        step_results: List[Dict[str, Any]] = []
        for i, action in enumerate(protocol_steps):
            if isinstance(action, dict):
                if action["action_name"] == "wait":
                    await asyncio.sleep(float(action["action_kwargs"]["time"]))
                    step_results.append({"step": i + 1, "action": "wait", "result": "completed"})
                    continue
                try:
                    result = await self._execute_single(**action)
                    step_results.append(
                        {"step": i + 1, "action": action["action_name"], "result": result}
                    )
                except WorkstationNodeTempError as ex:
                    step_results.append(
                        {"step": i + 1, "action": action["action_name"], "result": ex.args[0]}
                    )
            elif isinstance(action, list):
                results = await asyncio.gather(
                    *(self._execute_single(**parallel_action) for parallel_action in action)
                )
                step_results.append(
                    {
                        "step": i + 1,
                        "parallel_actions": [parallel_action["action_name"] for parallel_action in action],
                        "results": list(results),
                    }
                )

        await update_protocol_resources(
            host, [kwargs[field] for field in resource_fields if field in kwargs]
        )

        log.info(f"[Workstation] 协议 {protocol_name} 完成")
        return {
            "protocol_name": protocol_name,
            "steps_executed": len(protocol_steps),
            "step_results": step_results,
            "total_time": time.time() - time_start,
        }

    async def _execute_single(
        self, device_id: str, action_name: str, action_kwargs: Dict[str, Any]
    ) -> Any:
        """执行单个协议步骤（失败即抛异常，由 HostLink 结果规范化标 failed）。"""
        if action_name == "log_message":
            self._host.lab_logger().info(f"[Protocol Log] {action_kwargs}")
            raise WorkstationNodeTempError(f"[Protocol Log] {action_kwargs}")
        target = self._host.device_id if device_id in ("", None, "self") else device_id
        return await self._host.call_device_action_async(target, action_name, dict(action_kwargs))


__all__ = ["WorkstationNode"]
