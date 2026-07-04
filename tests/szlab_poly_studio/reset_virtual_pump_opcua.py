#!/usr/bin/env python3
"""将虚拟 pump 调试用的 OPC 变量复位到 CSV fixture 初始状态。"""

from __future__ import annotations

import argparse
import sys

from opcua import Client

DEFAULT_URL = "opc.tcp://127.0.0.1:48506/"

# 与 unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_nodes.csv 初始值一致
RESET_VALUES: dict[str, object] = {
    "S06准备信号": True,
    "S06允许加工": True,
    "S06工艺选择": 0,
    "S06_1号溶液添加量": 0,
    "S06_2号溶液添加量": 0,
    "S06参数写入完成": False,
    "S06加工完成": False,
    "传感器状态_上位机[3].NO[1]": True,
    "传感器状态_上位机[4].NO[12]": True,
    "传感器状态_上位机[5].NO[1]": True,
}


def find_virtual_mixer(client: Client):
    for child in client.get_objects_node().get_children():
        if child.get_browse_name().Name == "VirtualMixer":
            return child
    raise RuntimeError("未找到 VirtualMixer，请确认伪 OPC server 已启动且 URL 正确")


def reset_opcua(url: str) -> None:
    client = Client(url)
    client.connect()
    try:
        vm = find_virtual_mixer(client)
        nodes = {ch.get_browse_name().Name: ch for ch in vm.get_children()}
        missing = [name for name in RESET_VALUES if name not in nodes]
        if missing:
            raise RuntimeError(f"伪 server 缺少节点: {', '.join(missing)}")

        print(f"连接: {url}")
        for name, value in RESET_VALUES.items():
            nodes[name].set_value(value)
            print(f"  {name} = {value!r}")
        print("复位完成。")
    finally:
        client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description="复位虚拟 pump OPC 变量")
    parser.add_argument("--url", default=DEFAULT_URL, help="伪 OPC UA 地址")
    args = parser.parse_args()
    try:
        reset_opcua(args.url)
    except Exception as exc:
        print(f"复位失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
