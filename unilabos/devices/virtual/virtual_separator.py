import asyncio
import logging
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from unilabos.device_runtime.node import DeviceNode


class VirtualSeparator:
    """Virtual separator device for SeparateProtocol testing"""
    
    _ros_node: "DeviceNode"

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        # 处理可能的不同调用方式
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        # 设置默认值
        self.device_id = device_id or "unknown_separator"
        self.config = config or {}

        self.logger = logging.getLogger(f"VirtualSeparator.{self.device_id}")
        self.data = {}

        # 添加调试信息
        print(f"=== VirtualSeparator {self.device_id} is being created! ===")
        print(f"=== Config: {self.config} ===")
        print(f"=== Kwargs: {kwargs} ===")

        # 从config或kwargs中获取配置参数
        self.port = self.config.get("port") or kwargs.get("port", "VIRTUAL")
        self._volume = self.config.get("volume") or kwargs.get("volume", 250.0)
        self._has_phases = self.config.get("has_phases") or kwargs.get("has_phases", True)

        # 处理其他kwargs参数，但跳过已知的配置参数
        skip_keys = {"port", "volume", "has_phases"}
        for key, value in kwargs.items():
            if key not in skip_keys and not hasattr(self, key):
                setattr(self, key, value)
    
    def post_init(self, ros_node: "DeviceNode"):
        self._ros_node = ros_node

    async def initialize(self) -> bool:
        """Initialize virtual separator"""
        print(f"=== VirtualSeparator {self.device_id} initialize() called! ===")
        self.logger.info(f"Initializing virtual separator {self.device_id}")
        self.data.update(
            {
                "status": "Ready",
                "separator_state": "Ready",
                "volume": self._volume,
                "has_phases": self._has_phases,
                "phase_separation": False,
                "stir_speed": 0.0,
                "settling_time": 0.0,
                "progress": 0.0,
                "message": "",
            }
        )
        return True

    async def cleanup(self) -> bool:
        """Cleanup virtual separator"""
        self.logger.info(f"Cleaning up virtual separator {self.device_id}")
        return True

    async def separate(
        self,
        purpose: str,
        product_phase: str,
        from_vessel: str,
        separation_vessel: str,
        to_vessel: str,
        waste_phase_to_vessel: str = "",
        solvent: str = "",
        solvent_volume: float = 50.0,
        through: str = "",
        repeats: int = 1,
        stir_time: float = 30.0,
        stir_speed: float = 300.0,
        settling_time: float = 300.0,
    ) -> bool:
        """Execute separate action - matches Separate action"""
        self.logger.info(f"Separate: purpose={purpose}, product_phase={product_phase}, from_vessel={from_vessel}")

        # 验证参数
        if product_phase not in ["top", "bottom"]:
            self.logger.error(f"Invalid product_phase {product_phase}, must be 'top' or 'bottom'")
            self.data["message"] = f"产物相位 {product_phase} 无效，必须是 'top' 或 'bottom'"
            return False

        if purpose not in ["wash", "extract"]:
            self.logger.error(f"Invalid purpose {purpose}, must be 'wash' or 'extract'")
            self.data["message"] = f"分离目的 {purpose} 无效，必须是 'wash' 或 'extract'"
            return False

        # 开始分离
        self.data.update(
            {
                "status": "Running",
                "separator_state": "Separating",
                "purpose": purpose,
                "product_phase": product_phase,
                "from_vessel": from_vessel,
                "separation_vessel": separation_vessel,
                "to_vessel": to_vessel,
                "waste_phase_to_vessel": waste_phase_to_vessel,
                "solvent": solvent,
                "solvent_volume": solvent_volume,
                "repeats": repeats,
                "stir_speed": stir_speed,
                "settling_time": settling_time,
                "phase_separation": True,
                "progress": 0.0,
                "message": f"正在分离: {from_vessel} -> {to_vessel}",
            }
        )

        # 模拟分离过程
        total_time = (stir_time + settling_time) * repeats
        simulation_time = min(total_time / 60.0, 15.0)  # 最多模拟15秒

        for repeat in range(repeats):
            # 搅拌阶段
            for progress in range(0, 51, 10):
                await self._ros_node.sleep(simulation_time / (repeats * 10))
                overall_progress = ((repeat * 100) + (progress * 0.5)) / repeats
                self.data["progress"] = overall_progress
                self.data["message"] = f"第{repeat+1}次分离 - 搅拌中 ({progress}%)"

            # 静置分相阶段
            for progress in range(50, 101, 10):
                await self._ros_node.sleep(simulation_time / (repeats * 10))
                overall_progress = ((repeat * 100) + (progress * 0.5)) / repeats
                self.data["progress"] = overall_progress
                self.data["message"] = f"第{repeat+1}次分离 - 静置分相中 ({progress}%)"

        # 分离完成
        self.data.update(
            {
                "status": "Ready",
                "separator_state": "Ready",
                "phase_separation": False,
                "stir_speed": 0.0,
                "progress": 100.0,
                "message": f"分离完成: {repeats}次分离操作",
            }
        )

        self.logger.info(f"Separation completed: {repeats} cycles from {from_vessel} to {to_vessel}")
        return True

    # 状态属性
    @property
    def status(self) -> str:
        return self.data.get("status", "Unknown")

    @property
    def separator_state(self) -> str:
        return self.data.get("separator_state", "Unknown")

    @property
    def volume(self) -> float:
        return self.data.get("volume", self._volume)

    @property
    def has_phases(self) -> bool:
        return self.data.get("has_phases", self._has_phases)

    @property
    def phase_separation(self) -> bool:
        return self.data.get("phase_separation", False)

    @property
    def stir_speed(self) -> float:
        return self.data.get("stir_speed", 0.0)

    @property
    def settling_time(self) -> float:
        return self.data.get("settling_time", 0.0)

    @property
    def progress(self) -> float:
        return self.data.get("progress", 0.0)

    @property
    def message(self) -> str:
        return self.data.get("message", "")
