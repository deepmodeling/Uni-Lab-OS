"""SZLab S05 拍照工位单独调试脚本。

运行::

    PYTHONPATH=. python -m unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.debug_photoshotting

模式（``--mode``）:

- ``serve``: 只启动伪 OPC UA，供外部 UI/脚本连接。
- ``run``: 只执行配置里的 action，连接真机或已有伪 OPC UA。
- ``all``: 启动伪 OPC UA 后执行 action，适合本地一键调试。
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

_DEVICE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DEVICE_DIR.parents[4]
_DEFAULT_CONFIG = _DEVICE_DIR / "photoshotting_debug.json"


class DirectOpcUaGateway:
    def __init__(self, url: str, node_id_map: dict[str, str]) -> None:
        from unilabos.devices.workstation.szlab_poly_studio.s06_pump.opcua_client import SzlabMixerOpcUaClient

        self._client = SzlabMixerOpcUaClient(url=url, node_id_map=node_id_map)

    def read_variable(self, name: str, use_cache: bool = False) -> Any:
        del use_cache
        return self._client.read(name)

    def disconnect(self) -> None:
        self._client.disconnect()


def resolve_repo_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    for root in (_REPO_ROOT, _DEVICE_DIR):
        resolved = (root / candidate).resolve()
        if resolved.exists():
            return resolved
    return (_REPO_ROOT / candidate).resolve()


def load_photoshotting_debug_config(config_path: Path, *, use_production: bool) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    opcua = config["opcua"]
    config["resolved_opcua_url"] = opcua["production_url" if use_production else "virtual_url"]
    config["use_production"] = use_production
    virtual = config.get("virtual_opcua", {})
    config["virtual_csv_path"] = resolve_repo_path(virtual.get("csv", "photoshotting_nodes.csv"))
    config["virtual_flow_path"] = resolve_repo_path(virtual.get("flow", "photoshotting_flow.json"))
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
            "虚拟 OPC endpoint 必须绑定本机地址，例如 opc.tcp://127.0.0.1:48505/；"
            f"当前是 {endpoint}。真机地址请只写在 production_url，并使用 run + --production。"
        )

    print("启动 S05 伪 OPC UA:")
    print(f"  --csv {csv_path}")
    print(f"  --endpoint {endpoint}")
    print(f"  --flow {flow_path}")

    server = OpcUaCsvServer(endpoint=endpoint, csv_path=csv_path)
    server.start()
    daemon = OpcUaFlowDaemon(url=endpoint, flow_path=flow_path)
    daemon.start()
    print("S05 伪 OPC 已启动，按 Ctrl+C 停止")
    return server, daemon


def run_photoshotting_debug(
    *,
    opcua_url: str,
    timeout: float,
    csv_path: str | None,
    save_dir: str,
    action: Literal["take_photo", "take_dual_view_photos"],
    sample_id: str,
    photo_path: str,
    top_photo_path: str,
    side_photo_path: str,
    inspection_result: str,
    algorithm_url: str,
    algorithm_timeout: float,
    require_material: bool,
    opcua_node_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting import (
        SzlabMixerPhotoShottingDevice,
    )

    print(f"OPC UA: {opcua_url}")
    print(f"Action: {action}")
    print(f"CSV: {csv_path}")
    gateway = DirectOpcUaGateway(opcua_url, opcua_node_id_map) if opcua_node_id_map else None
    device = SzlabMixerPhotoShottingDevice(
        url=opcua_url,
        timeout=timeout,
        csv_path=csv_path,
        save_dir=save_dir,
        use_plc_gateway=gateway is not None,
    )
    if gateway is not None:
        device.set_plc_gateway(gateway)
    try:
        if action == "take_photo":
            result = device.take_photo(
                sample_id=sample_id,
                photo_path=photo_path,
                inspection_result=inspection_result,
                require_material=require_material,
            )
        else:
            result = device.take_dual_view_photos(
                sample_id=sample_id,
                top_photo_path=top_photo_path,
                side_photo_path=side_photo_path,
                algorithm_url=algorithm_url,
                algorithm_timeout=algorithm_timeout,
                require_material=require_material,
            )
        print(result)
        return result
    finally:
        device.disconnect()
        if gateway is not None:
            gateway.disconnect()


def _run_from_config(config_path: Path, *, use_production: bool) -> dict[str, Any]:
    config = load_photoshotting_debug_config(config_path, use_production=use_production)
    device_cfg = config["device"]
    action_cfg = config["action"]
    action_name = str(action_cfg["name"])
    if action_name not in ("take_photo", "take_dual_view_photos"):
        raise ValueError(f"未知 action: {action_name}")

    print(f"Config file: {config_path.resolve()}")
    print(f"Raw action config: {action_cfg}")
    return run_photoshotting_debug(
        opcua_url=config["resolved_opcua_url"],
        timeout=float(device_cfg["timeout"]),
        csv_path=(
            str(device_cfg.get("csv_path", "szlab_plc_0623.csv"))
            if use_production
            else str(config["virtual_csv_path"])
        ),
        save_dir=str(device_cfg["save_dir"]),
        action=action_name,  # type: ignore[arg-type]
        sample_id=str(action_cfg.get("sample_id", "")),
        photo_path=str(action_cfg.get("photo_path", "")),
        top_photo_path=str(action_cfg.get("top_photo_path", "")),
        side_photo_path=str(action_cfg.get("side_photo_path", "")),
        inspection_result=str(action_cfg.get("inspection_result", "")),
        algorithm_url=str(action_cfg.get("algorithm_url", "")),
        algorithm_timeout=float(action_cfg.get("algorithm_timeout", 10.0)),
        require_material=bool(action_cfg.get("require_material", False)),
        opcua_node_id_map=(
            dict(device_cfg.get("opcua_node_id_map", {}))
            if use_production and device_cfg.get("opcua_node_id_map")
            else None
        ),
    )


def serve_virtual_opcua(config_path: Path = _DEFAULT_CONFIG) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_photoshotting_debug_config(config_path, use_production=False)
    server, daemon = start_virtual_opcua_stack(config)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止 S05 伪 OPC ...")
        daemon.stop()
        server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="SZLab S05 拍照工位本地调试")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=("serve", "run", "all"), default="all")
    parser.add_argument("--production", action="store_true", help="连接 photoshotting_debug.json 中的 production_url")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.mode == "serve":
        serve_virtual_opcua(args.config)
        return
    if args.mode == "run":
        _run_from_config(args.config, use_production=args.production)
        return

    config = load_photoshotting_debug_config(args.config, use_production=False)
    server, daemon = start_virtual_opcua_stack(config)
    try:
        time.sleep(0.2)
        _run_from_config(args.config, use_production=False)
    finally:
        daemon.stop()
        server.stop()


if __name__ == "__main__":
    main()
