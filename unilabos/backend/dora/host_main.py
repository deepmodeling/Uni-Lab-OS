"""Dora 实验性监控 host 节点，不属于公开 runtime backend。

订阅所有设备的状态输出并周期性汇总打印。它不参与性能测试——
性能测试由 `unilabos/backend/dora/benchmark/host.py` 中的专用 host 承担。

环境变量：
    UNILAB_DORA_DEVICES : 设备 id 列表的 JSON 数组
"""

from __future__ import annotations

import json
import logging
import os

from unilabos.backend.dora.serialization import decode


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("unilabos.backend.dora.host")
    devices = json.loads(os.environ.get("UNILAB_DORA_DEVICES", "[]"))
    log.info(f"dora host 启动，监控设备: {devices}")

    from dora import Node

    node = Node()
    latest = {}
    for event in node:
        etype = event["type"]
        if etype == "INPUT":
            eid = event["id"]
            if eid == "tick":
                if latest:
                    log.info(f"设备状态汇总: {json.dumps(latest, ensure_ascii=False)}")
            elif eid.endswith("__status"):
                try:
                    msg = decode(event["value"])
                    latest[msg.get("device", eid)] = msg.get("body", {})
                except Exception as exc:
                    log.warning(f"状态解码失败: {exc}")
        elif etype in ("STOP", "ERROR"):
            break


if __name__ == "__main__":
    main()
