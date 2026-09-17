import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class VirtualPrinter:
    _ros_node: BaseROS2DeviceNode

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        self.device_id = device_id or "virtual_printer"
        self.config = config or {}

        self.logger = logging.getLogger(f"VirtualPrinter.{self.device_id}")
        self.data: Dict[str, Any] = {}

        self.port = self.config.get("port") or kwargs.get("port", "VIRTUAL")
        self.prefix = self.config.get("prefix") or kwargs.get("prefix", "[VIRTUAL-PRINTER]")
        self.pretty = bool(self.config.get("pretty", True))

        print(f"{self.prefix} created: id={self.device_id}, port={self.port}")

    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node

    async def initialize(self) -> bool:
        self.data.update(
            {
                "status": "Idle",
                "message": "Ready",
                "last_received": None,
                "received_count": 0,
            }
        )
        self.logger.info("Initialized")
        return True

    async def cleanup(self) -> bool:
        self.data.update({"status": "Offline", "message": "System offline"})
        self.logger.info("Cleaned up")
        return False

    async def print_message(self, content: Any = None, **kwargs) -> Dict[str, Any]:
        """打印虚拟设备接收到的内容（推荐 action）"""
        await self._record_and_print(action="print_message", content=content, kwargs=kwargs)
        return {"success": True, "message": "printed", "return_info": "printed"}

    async def receive(self, *args, **kwargs) -> Dict[str, Any]:
        payload = {"args": list(args), "kwargs": kwargs}
        await self._record_and_print(action="receive", content=payload, kwargs={})
        return {"success": True, "message": "received", "return_info": "received"}

    async def _record_and_print(self, action: str, content: Any, kwargs: Dict[str, Any]) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        record = {
            "timestamp": ts,
            "device_id": self.device_id,
            "action": action,
            "content": content,
            "kwargs": kwargs,
        }

        self.data["last_received"] = record
        self.data["received_count"] = int(self.data.get("received_count", 0)) + 1
        self.data["status"] = "Idle"
        self.data["message"] = f"Last action: {action} @ {ts}"

        if self.pretty:
            try:
                txt = json.dumps(record, ensure_ascii=False, indent=2, default=str)
            except Exception:
                txt = str(record)
        else:
            txt = str(record)

        print(f"{self.prefix} received:\n{txt}")
        self.logger.info("Received: %s", record)

    @property
    def status(self) -> str:
        return self.data.get("status", "Unknown")

    @property
    def message(self) -> str:
        return self.data.get("message", "")

    @property
    def last_received(self) -> Any:
        return self.data.get("last_received")

    @property
    def received_count(self) -> int:
        return int(self.data.get("received_count", 0))