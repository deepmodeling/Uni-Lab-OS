import asyncio
import time
from typing import Dict, Any, Optional


class VirtualVacuumPump:
    """Virtual vacuum pump for testing"""

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        self.device_id = device_id or "unknown_vacuum_pump"
        self.config = config or {}
        self.data = {}
        self._status = "OPEN"

    async def initialize(self) -> bool:
        """Initialize virtual vacuum pump"""
        self.data.update({
            "status": self._status
        })
        return True

    async def cleanup(self) -> bool:
        """Cleanup virtual vacuum pump"""
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

    def is_open(self):
        return self._status

    def is_closed(self):
        return not self._status
