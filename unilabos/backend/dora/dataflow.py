"""从设备清单生成 dora dataflow（YAML）。

设备清单元素结构：
    {"id": str, "driver": "module.path:ClassName", "config": dict}

生成的 dataflow 拓扑：
    每个设备一个节点：tick(定时器)->发布状态；订阅 host 下发的 cmd_<id>；
    host 节点：订阅所有设备的 status/reply/stream/action_result；对每个设备输出 cmd_<id>。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import yaml

# 设备节点对外的输出通道
DEVICE_OUTPUTS = ["status", "reply", "stream", "action_result"]


def build_dataflow(
    devices: List[Dict[str, Any]],
    *,
    host_module: str,
    host_id: str = "host",
    device_tick_ms: int = 100,
    host_tick_ms: int = 1000,
    host_env: Optional[Dict[str, str]] = None,
    device_inputs_on_host: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """构造 dora dataflow 字典。"""
    if device_inputs_on_host is None:
        device_inputs_on_host = DEVICE_OUTPUTS

    nodes: List[Dict[str, Any]] = []

    # 设备节点
    for dev in devices:
        dev_id = dev["id"]
        nodes.append(
            {
                "id": dev_id,
                "path": "python",
                "args": "-m unilabos.backend.dora.node_main",
                "env": {
                    "UNILAB_DORA_DEVICE_ID": dev_id,
                    "UNILAB_DORA_DRIVER": dev["driver"],
                    "UNILAB_DORA_CONFIG": json.dumps(dev.get("config", {}), ensure_ascii=False),
                },
                "inputs": {
                    "tick": f"dora/timer/millis/{device_tick_ms}",
                    "cmd": f"{host_id}/cmd_{dev_id}",
                },
                "outputs": list(DEVICE_OUTPUTS),
            }
        )

    # host 节点
    host_inputs: Dict[str, str] = {"tick": f"dora/timer/millis/{host_tick_ms}"}
    for dev in devices:
        dev_id = dev["id"]
        for out in device_inputs_on_host:
            host_inputs[f"{dev_id}__{out}"] = f"{dev_id}/{out}"

    host_outputs = [f"cmd_{dev['id']}" for dev in devices]

    env = {"UNILAB_DORA_DEVICES": json.dumps([d["id"] for d in devices], ensure_ascii=False)}
    if host_env:
        env.update(host_env)

    nodes.append(
        {
            "id": host_id,
            "path": "python",
            "args": f"-m {host_module}",
            "env": env,
            "inputs": host_inputs,
            "outputs": host_outputs,
        }
    )

    return {"nodes": nodes}


def write_dataflow(path: str, dataflow: Dict[str, Any]) -> str:
    """把 dataflow 写为 YAML 文件，返回路径。"""
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(dataflow, f, allow_unicode=True, sort_keys=False)
    return path
