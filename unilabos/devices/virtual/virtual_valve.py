import asyncio
import logging
from typing import Dict, Any


class VirtualValve:
    """Virtual valve device for AddProtocol testing"""

    def __init__(self, device_id: str = None, config: Dict[str, Any] = None, **kwargs):
        # 处理可能的不同调用方式
        if device_id is None and 'id' in kwargs:
            device_id = kwargs.pop('id')
        if config is None and 'config' in kwargs:
            config = kwargs.pop('config')

        # 设置默认值
        self.device_id = device_id or "unknown_valve"
        self.config = config or {}

        self.logger = logging.getLogger(f"VirtualValve.{self.device_id}")
        self.data = {}

        print(f"=== VirtualValve {self.device_id} is being created! ===")
        print(f"=== Config: {self.config} ===")
        print(f"=== Kwargs: {kwargs} ===")

        # 处理所有配置参数，包括port
        self.port = self.config.get('port', 'VIRTUAL')
        self.positions = self.config.get('positions', 6)
        self.current_position = 0

        # 忽略其他可能的kwargs参数
        for key, value in kwargs.items():
            if not hasattr(self, key):
                setattr(self, key, value)

    async def initialize(self) -> bool:
        """Initialize virtual valve"""
        print(f"=== VirtualValve {self.device_id} initialize() called! ===")
        self.logger.info(f"Initializing virtual valve {self.device_id}")
        self.data.update({
            "status": "Idle",
            "valve_state": "Closed",
            "current_position": 0,
            "target_position": 0,
            "max_positions": self.positions
        })
        return True

    async def cleanup(self) -> bool:
        """Cleanup virtual valve"""
        self.logger.info(f"Cleaning up virtual valve {self.device_id}")
        return True

    async def set_position(self, position: int) -> bool:
        """Set valve position - matches SendCmd action"""
        if 0 <= position <= self.positions:
            self.logger.info(f"Setting valve position to {position}")
            self.data.update({
                "target_position": position,
                "current_position": position,
                "valve_state": "Open" if position > 0 else "Closed"
            })
            return True
        else:
            self.logger.error(f"Invalid position {position}. Must be 0-{self.positions}")
            return False

    async def open(self) -> bool:
        """Open valve - matches EmptyIn action"""
        self.logger.info("Opening valve")
        self.data.update({
            "valve_state": "Open",
            "current_position": 1
        })
        return True

    async def close(self) -> bool:
        """Close valve - matches EmptyIn action"""
        self.logger.info("Closing valve")
        self.data.update({
            "valve_state": "Closed",
            "current_position": 0
        })
        return True

    # 状态属性
    @property
    def status(self) -> str:
        return self.data.get("status", "Unknown")

    @property
    def valve_state(self) -> str:
        return self.data.get("valve_state", "Unknown")

    @property
    def current_position(self) -> int:
        return self.data.get("current_position", 0)

    @property
    def target_position(self) -> int:
        return self.data.get("target_position", 0)

    @property
    def max_positions(self) -> int:
        return self.data.get("max_positions", 6)
