import time
from typing import Dict, Any, Optional

# set_status 收到的开关字符串（协议编译器发 ON / OFF）
_OPEN_STATES = frozenset({"OPEN", "ON"})
_CLOSED_STATES = frozenset({"CLOSED", "CLOSE", "OFF"})


class VirtualGasSource:
    """Virtual gas source for testing"""

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        self.device_id = device_id or "unknown_gas_source"
        # 两种 backend 的传参形状都接受：config 字典或平铺的构造参数
        self.config = {**kwargs, **(config or {})}
        self.data = {}
        self._status = "OPEN"

    async def initialize(self) -> bool:
        """Initialize virtual gas source"""
        self.data.update({
            "status": self._status
        })
        return True

    async def cleanup(self) -> bool:
        """Cleanup virtual gas source"""
        return True

    @property
    def status(self) -> str:
        return self._status

    def get_status(self) -> str:
        return self._status

    def set_status(self, string):
        self._status = string
        time.sleep(5)

    def open(self):
        self._status = "OPEN"

    def close(self):
        self._status = "CLOSED"

    def is_open(self) -> bool:
        """气源是否打开"""
        return str(self._status).upper() in _OPEN_STATES

    def is_closed(self) -> bool:
        """气源是否关闭"""
        return str(self._status).upper() in _CLOSED_STATES
