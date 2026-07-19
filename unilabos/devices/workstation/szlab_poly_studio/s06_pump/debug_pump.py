"""SZLab 注射泵单独调试脚本。

改 ``pump_debug.json`` 里的 action / 阀位；``if __name__`` 里选模式和虚拟/真机。

模式（``debug_mode``）::

    serve  - 只启动伪 OPC（csv server + flow daemon），保持运行
    run    - 只执行 action（需伪 OPC 已启动，或先用 serve）
    all    - 先启动伪 OPC，再执行 action（虚拟模式一键调试）

运行::

    PYTHONPATH=. python -m unilabos.devices.workstation.szlab_poly_studio.s06_pump.debug_pump
"""

from __future__ import annotations

import json
import logging
import time
import argparse
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

_DEVICE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DEVICE_DIR.parents[4]
_DEFAULT_CONFIG = _DEVICE_DIR / "pump_debug.json"
logger = logging.getLogger(__name__)


def resolve_repo_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    for root in (_REPO_ROOT, _DEVICE_DIR):
        resolved = (root / candidate).resolve()
        if resolved.exists():
            return resolved
    return (_REPO_ROOT / candidate).resolve()


def load_pump_debug_config(
    config_path: Path,
    *,
    use_production: bool,
) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    opcua = config["opcua"]
    config["resolved_opcua_url"] = opcua["production_url" if use_production else "virtual_url"]
    config["use_production"] = use_production
    virtual = config.get("virtual_opcua", {})
    config["virtual_csv_path"] = resolve_repo_path(virtual.get("csv", "pump_nodes.csv"))
    config["virtual_flow_path"] = resolve_repo_path(virtual.get("flow", "pump_flow.json"))
    config["virtual_endpoint"] = virtual.get("endpoint", config["resolved_opcua_url"])
    return config


def start_virtual_opcua_stack(config: dict[str, Any]) -> tuple[Any, Any]:
    from tests.pseudo_devices.common.opcua_csv_server import OpcUaCsvServer
    from tests.pseudo_devices.common.opcua_flow_daemon import OpcUaFlowDaemon

    csv_path = config["virtual_csv_path"]
    flow_path = config["virtual_flow_path"]
    endpoint = config["virtual_endpoint"]
    endpoint_host = urlparse(endpoint).hostname
    if endpoint_host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise ValueError(
            "虚拟 OPC endpoint 必须绑定本机地址，例如 opc.tcp://127.0.0.1:48506/；"
            f"当前是 {endpoint}。真机地址请只写在 production_url，并使用 run + use_production=True。"
        )

    print("启动伪 OPC UA:")
    print(f"  --csv {csv_path}")
    print(f"  --endpoint {endpoint}")
    print(f"  --flow {flow_path}")

    server = OpcUaCsvServer(endpoint=endpoint, csv_path=csv_path)
    server.start()
    daemon = OpcUaFlowDaemon(url=endpoint, flow_path=flow_path)
    daemon.start()
    print("伪 OPC 已启动，按 Ctrl+C 停止")
    return server, daemon


def serve_virtual_opcua(config_path: Path = _DEFAULT_CONFIG) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_pump_debug_config(config_path, use_production=False)
    server, daemon = start_virtual_opcua_stack(config)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止伪 OPC ...")
        daemon.stop()
        server.stop()


def optional_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def run_pump_debug(
    *,
    opcua_url: str,
    timeout: float,
    pipeline_route_specs: list[dict[str, Any]],
    opcua_node_id_map: dict[str, str],
    opcua_allow_recursive_browse: bool,
    action: Literal["transfer_liquid", "run_solvent_addition"],
    pump: int,
    volume: int,
    volume_pump_1: int,
    volume_pump_2: int,
    direction: Literal["aspirate", "dispense"],
    pipeline: Literal["aspirate", "dispense", "air"],
    skip_level_check: bool,
    beaker_true_means_present: bool,
) -> dict[str, Any]:
    import unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump as pump_module
    from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice

    print(f"OPC UA: {opcua_url}")
    print(f"Action: {action}")
    print(f"Pump module: {pump_module.__file__}")
    print(
        "Resolved action volumes: "
        f"pump={pump}, volume={volume}, volume_pump_1={volume_pump_1}, volume_pump_2={volume_pump_2}"
    )

    device = SzlabMixerPumpDevice(
        url=opcua_url,
        timeout=timeout,
        pipeline_route_specs=pipeline_route_specs,
        opcua_browse_depth=8,
        opcua_browse_limit=10000,
        opcua_node_id_map=opcua_node_id_map,
        opcua_allow_recursive_browse=opcua_allow_recursive_browse,
    )
    try:
        if action == "transfer_liquid":
            result = device.transfer_liquid(
                process=pump,
                volume=volume,
                direction=direction,
                pipeline=pipeline,
            )
        else:
            result = device.run_solvent_addition(
                process=pump,
                volume_pump_1=volume_pump_1,
                volume_pump_2=volume_pump_2,
                skip_level_check=skip_level_check,
                beaker_true_means_present=beaker_true_means_present,
            )
        print(result)
        return result
    finally:
        device.disconnect()


def _run_from_config(config_path: Path, *, use_production: bool) -> dict[str, Any]:
    config = load_pump_debug_config(config_path, use_production=use_production)
    device_cfg = config["device"]
    action_cfg = config["action"]
    action_name = str(action_cfg["name"])
    if action_name not in ("transfer_liquid", "run_solvent_addition"):
        raise ValueError(f"未知 action: {action_name}")

    if not use_production:
        print("虚拟 OPC 配置:")
        print(f"  csv: {config['virtual_csv_path']}")
        print(f"  endpoint: {config['virtual_endpoint']}")
        print(f"  flow: {config['virtual_flow_path']}")
    print(f"Config file: {config_path.resolve()}")
    print(f"Raw action config: {action_cfg}")

    return run_pump_debug(
        opcua_url=config["resolved_opcua_url"],
        timeout=float(device_cfg["timeout"]),
        pipeline_route_specs=list(device_cfg.get("pipeline_route_specs", [])),
        opcua_node_id_map=dict(device_cfg.get("opcua_node_id_map", {})) if use_production else {},
        opcua_allow_recursive_browse=(
            bool(device_cfg.get("opcua_allow_recursive_browse", False)) if use_production else False
        ),
        action=action_name,  # type: ignore[arg-type]
        pump=int(action_cfg["pump"]),
        volume=int(action_cfg.get("volume", 1)),
        volume_pump_1=optional_int(action_cfg.get("volume_pump_1")),
        volume_pump_2=optional_int(action_cfg.get("volume_pump_2")),
        direction=action_cfg.get("direction", "aspirate"),
        pipeline=action_cfg.get("pipeline", "aspirate"),
        skip_level_check=bool(action_cfg["skip_level_check"]),
        beaker_true_means_present=bool(action_cfg["beaker_true_means_present"]),
    )


def reset_plc_signals(config_path: Path, *, use_production: bool) -> None:
    from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
    from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import (
        S06_PARAM_WRITTEN_VAR,
        S06_PROCESS_SELECT_VAR,
        s06_solution_amount_var,
    )

    config = load_pump_debug_config(config_path, use_production=use_production)
    device_cfg = config["device"]
    node_id_map = dict(device_cfg.get("opcua_node_id_map", {})) if use_production else {}
    client = SZLabPolyPLCDevice(
        url=config["resolved_opcua_url"],
        csv_path=False,
        opcua_object_name="VirtualMixer",
        node_id_map=node_id_map,
    )
    try:
        for name, value in (
            (S06_PARAM_WRITTEN_VAR, False),
            (S06_PROCESS_SELECT_VAR, 0),
            (s06_solution_amount_var(1), 0),
            (s06_solution_amount_var(2), 0),
        ):
            if use_production and name not in node_id_map:
                print(f"跳过 {name}：真机配置中没有 NodeId（可能未公有发布）")
                continue
            if use_production:
                accessible, detail = client.check_variable_accessible(name)
                if not accessible:
                    print(f"跳过 {name}：NodeId 无效或不可访问 ({detail})")
                    continue
            try:
                client.write(name, value)
                print(f"已清除 {name} = {value!r}")
            except Exception as exc:
                print(f"清除 {name} 失败：{exc}")
    finally:
        client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description="SZLab 注射泵单独调试")
    parser.add_argument(
        "--mode",
        choices=("serve", "run", "all", "reset"),
        default="run",
        help="serve=只起伪 OPC；run=执行 action；all=虚拟一键执行；reset=清除 PLC 侧 PC->PLC 信号",
    )
    parser.add_argument("--production", action="store_true", help="连接真机 production_url")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG, help="调试配置 JSON")
    args = parser.parse_args()

    debug_mode = args.mode
    use_production = args.production
    config_path = args.config

    if debug_mode == "serve":
        if use_production:
            raise SystemExit("serve 模式仅用于虚拟 OPC，请将 use_production 设为 False")
        serve_virtual_opcua(config_path)
        return 0

    if debug_mode == "reset":
        reset_plc_signals(config_path, use_production=use_production)
        return 0

    virtual_stack: tuple[Any, Any] | None = None
    if debug_mode == "all" and not use_production:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        config = load_pump_debug_config(config_path, use_production=False)
        virtual_stack = start_virtual_opcua_stack(config)
        time.sleep(0.5)

    try:
        result = _run_from_config(config_path, use_production=use_production)
    finally:
        if virtual_stack is not None:
            daemon, server = virtual_stack[1], virtual_stack[0]
            daemon.stop()
            server.stop()

    if not result.get("success"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
