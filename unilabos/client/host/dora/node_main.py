"""dora 设备节点通用入口。

每个设备对应一个 dora 节点进程，由 dataflow 通过环境变量参数化：
    UNILAB_DORA_DEVICE_ID : 设备 id
    UNILAB_DORA_DRIVER    : 驱动类，形如 `module.path:ClassName`
    UNILAB_DORA_CONFIG    : 可选，设备 config 的 JSON 字符串
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys


def load_driver_class(spec: str):
    """按 `module.path:ClassName` 加载驱动类。"""
    module_path, _, cls_name = spec.partition(":")
    if not module_path or not cls_name:
        raise ValueError(f"非法的驱动 spec: {spec!r}，应为 'module.path:ClassName'")
    module = importlib.import_module(module_path)
    return getattr(module, cls_name)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    device_id = os.environ.get("UNILAB_DORA_DEVICE_ID", "device")
    driver_spec = os.environ.get("UNILAB_DORA_DRIVER")
    if not driver_spec:
        print("缺少 UNILAB_DORA_DRIVER 环境变量", file=sys.stderr)
        sys.exit(1)

    config = {}
    raw_config = os.environ.get("UNILAB_DORA_CONFIG")
    if raw_config:
        try:
            config = json.loads(raw_config)
        except json.JSONDecodeError:
            config = {}

    driver_cls = load_driver_class(driver_spec)
    driver = driver_cls(device_id=device_id, config=config)

    # 延迟导入，确保 driver 先完成（driver import 可能较慢）
    from unilabos.backend.dora.dora_device_node import DoraDeviceNode

    DoraDeviceNode(driver, device_id).run()


if __name__ == "__main__":
    main()
