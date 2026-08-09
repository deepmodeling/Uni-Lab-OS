"""dora 后端入口，与 app/backend.py:start_backend 对接。

`main` / `slave` 的位置参数签名与 ROS 后端保持一致。它从 devices_config 生成
dora dataflow（每个设备一个 dora 节点 + 一个监控 host），用 `dora run` 启动并守护。
"""

from __future__ import annotations

import importlib.util
import tempfile
import time
from typing import Any, Dict, List, Optional

from unilabos.dora import dataflow as dataflow_mod
from unilabos.dora import runtime
from unilabos.utils import logger


def validate_environment() -> None:
    """Dora 依赖缺失时，在 backend 线程启动前直接失败。"""

    missing = []
    if runtime.dora_binary() is None:
        missing.append("dora CLI（执行 `cargo install dora-cli`）")
    if importlib.util.find_spec("dora") is None:
        missing.append("dora Python API（安装 `dora-rs`）")
    if importlib.util.find_spec("pyarrow") is None:
        missing.append("pyarrow")
    if missing:
        raise RuntimeError(
            "Dora backend 缺少依赖：" + "，".join(missing)
        )


def _resolve_devices(devices_config) -> List[Dict[str, Any]]:
    """从 ResourceTreeSet 解析出 dora 设备清单（含驱动 module:Class）。"""
    from unilabos.registry.registry import lab_registry

    devices: List[Dict[str, Any]] = []
    for node in devices_config.root_nodes:
        rc = node.res_content
        if getattr(rc, "type", None) != "device":
            continue
        klass = rc.klass
        if not isinstance(klass, str) or klass not in lab_registry.device_type_registry:
            logger.warning(f"[dora] 设备 {rc.id} 的类 {klass} 未在注册表中，跳过")
            continue
        entry = lab_registry.device_type_registry[klass]
        class_conf = entry.get("class", {})
        module_spec = class_conf.get("module") if isinstance(class_conf, dict) else None
        if not module_spec or ":" not in module_spec:
            logger.warning(f"[dora] 设备 {rc.id} 缺少可用的 module:Class（{module_spec}），跳过")
            continue
        devices.append(
            {"id": rc.id, "driver": module_spec, "config": getattr(rc, "config", {}) or {}}
        )
    return devices


def _launch(devices: List[Dict[str, Any]], device_tick_ms: int = 100, persistent: bool = True) -> None:
    if not devices:
        logger.warning("[dora] 没有可用的非 ROS 设备，dora 后端空转")
        while True:
            time.sleep(1)

    df = dataflow_mod.build_dataflow(
        devices,
        host_module="unilabos.dora.host_main",
        device_tick_ms=device_tick_ms,
        host_tick_ms=2000,
        device_inputs_on_host=["status"],  # 监控 host 只需状态
    )
    tmp = tempfile.NamedTemporaryFile("w", suffix="_dora_dataflow.yml", delete=False)
    tmp.close()
    path = dataflow_mod.write_dataflow(tmp.name, df)
    logger.info(f"[dora] dataflow 已生成: {path}（{len(devices)} 台设备）")

    if persistent:
        # 常驻模式：daemon 常驻 + 预建图 + start，冷启动编排从关键路径摊薄
        if not runtime.ensure_up():
            logger.warning("[dora] 常驻 daemon 启动失败，回退到 `dora run` 自包含模式")
            persistent = False
        else:
            logger.info("[dora] coordinator/daemon 已就绪，预建图中...")
            build = runtime.build_dataflow(path)
            if build.returncode != 0:
                logger.warning(f"[dora] dora build 失败，回退 run 模式: {build.stderr[:300]}")
                persistent = False
            else:
                name = f"unilab_{int(time.time())}"
                start = runtime.start_dataflow(path, name=name, detach=True)
                if start.returncode != 0:
                    logger.warning(f"[dora] dora start 失败，回退 run 模式: {start.stderr[:300]}")
                    persistent = False
                else:
                    logger.info(f"[dora] dataflow 已在常驻 daemon 上启动 name={name}")
                    try:
                        while runtime.is_up():
                            time.sleep(2)
                    except KeyboardInterrupt:
                        pass
                    finally:
                        runtime.stop_dataflow(name)
                    return

    if not persistent:
        # 自包含模式：dora run 一把梭（内部拉起 coordinator/daemon）
        proc = runtime.run_dataflow(path)
        logger.info(f"[dora] dora run 已启动 pid={proc.pid}")
        try:
            proc.wait()
        except KeyboardInterrupt:
            runtime.terminate_process(proc)


def main(
    devices_config,
    resources_config,
    resources_edge_config: list = [],
    graph: Optional[Dict[str, Any]] = None,
    controllers_config: Dict[str, Any] = {},
    bridges: List[Any] = [],
    visual: str = "disable",
    resources_mesh_config: dict = {},
    *args,
    **kwargs,
) -> None:
    """dora 后端主节点。"""
    devices = _resolve_devices(devices_config)
    logger.info(f"[dora] 解析到 {len(devices)} 台设备: {[d['id'] for d in devices]}")
    _launch(devices)


def slave(
    devices_config,
    resources_config,
    resources_edge_config: list = [],
    graph: Optional[Dict[str, Any]] = None,
    controllers_config: Dict[str, Any] = {},
    bridges: List[Any] = [],
    visual: str = "disable",
    resources_mesh_config: dict = {},
    *args,
    **kwargs,
) -> None:
    """dora 后端从节点（当前与 main 行为一致，单机场景无需区分）。"""
    main(
        devices_config,
        resources_config,
        resources_edge_config,
        graph,
        controllers_config,
        bridges,
        visual,
        resources_mesh_config,
    )


if __name__ == "__main__":
    main(None, None)
