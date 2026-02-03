"""
Virtual Workbench Device - 模拟工作台设备
包含：
- 1个机械臂 (每次操作3s, 独占锁)
- 3个加热台 (每次加热10s, 可并行)

工作流程：
1. A1-A5 物料同时启动，竞争机械臂
2. 机械臂将物料移动到空闲加热台
3. 加热完成后，机械臂将物料移动到C1-C5

注意：调用来自线程池，使用 threading.Lock 进行同步
"""
import logging
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum
from threading import Lock, RLock

from typing_extensions import TypedDict

from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.utils.decorator import not_action


# ============ TypedDict 返回类型定义 ============

class MoveToHeatingStationResult(TypedDict):
    """move_to_heating_station 返回类型"""
    success: bool
    station_id: int
    material_id: str
    material_number: int
    message: str


class StartHeatingResult(TypedDict):
    """start_heating 返回类型"""
    success: bool
    station_id: int
    material_id: str
    material_number: int
    message: str


class MoveToOutputResult(TypedDict):
    """move_to_output 返回类型"""
    success: bool
    station_id: int
    material_id: str


class PrepareMaterialsResult(TypedDict):
    """prepare_materials 返回类型 - 批量准备物料"""
    success: bool
    count: int
    material_1: int  # 物料编号1
    material_2: int  # 物料编号2
    material_3: int  # 物料编号3
    material_4: int  # 物料编号4
    material_5: int  # 物料编号5
    message: str


# ============ 状态枚举 ============

class HeatingStationState(Enum):
    """加热台状态枚举"""
    IDLE = "idle"  # 空闲
    OCCUPIED = "occupied"  # 已放置物料，等待加热
    HEATING = "heating"  # 加热中
    COMPLETED = "completed"  # 加热完成，等待取走


class ArmState(Enum):
    """机械臂状态枚举"""
    IDLE = "idle"  # 空闲
    BUSY = "busy"  # 工作中


@dataclass
class HeatingStation:
    """加热台数据结构"""
    station_id: int
    state: HeatingStationState = HeatingStationState.IDLE
    current_material: Optional[str] = None  # 当前物料 (如 "A1", "A2")
    material_number: Optional[int] = None  # 物料编号 (1-5)
    heating_start_time: Optional[float] = None
    heating_progress: float = 0.0


class VirtualWorkbench:
    """
    Virtual Workbench Device - 虚拟工作台设备

    模拟一个包含1个机械臂和3个加热台的工作站
    - 机械臂操作耗时3秒，同一时间只能执行一个操作
    - 加热台加热耗时10秒，3个加热台可并行工作

    工作流:
    1. 物料A1-A5并发启动（线程池），竞争机械臂使用权
    2. 获取机械臂后，查找空闲加热台
    3. 机械臂将物料放入加热台，开始加热
    4. 加热完成后，机械臂将物料移动到目标位置Cn
    """

    _ros_node: BaseROS2DeviceNode

    # 配置常量
    ARM_OPERATION_TIME: float = 3.0  # 机械臂操作时间(秒)
    HEATING_TIME: float = 10.0  # 加热时间(秒)
    NUM_HEATING_STATIONS: int = 3  # 加热台数量

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        # 处理可能的不同调用方式
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        self.device_id = device_id or "virtual_workbench"
        self.config = config or {}

        self.logger = logging.getLogger(f"VirtualWorkbench.{self.device_id}")
        self.data: Dict[str, Any] = {}

        # 从config中获取可配置参数
        self.ARM_OPERATION_TIME = float(self.config.get("arm_operation_time", 3.0))
        self.HEATING_TIME = float(self.config.get("heating_time", 10.0))
        self.NUM_HEATING_STATIONS = int(self.config.get("num_heating_stations", 3))

        # 机械臂状态和锁 (使用threading.Lock)
        self._arm_lock = Lock()
        self._arm_state = ArmState.IDLE
        self._arm_current_task: Optional[str] = None

        # 加热台状态 (station_id -> HeatingStation) - 立即初始化，不依赖initialize()
        self._heating_stations: Dict[int, HeatingStation] = {
            i: HeatingStation(station_id=i)
            for i in range(1, self.NUM_HEATING_STATIONS + 1)
        }
        self._stations_lock = RLock()  # 可重入锁，保护加热台状态

        # 任务追踪
        self._active_tasks: Dict[str, Dict[str, Any]] = {}  # material_id -> task_info
        self._tasks_lock = Lock()

        # 处理其他kwargs参数
        skip_keys = {"arm_operation_time", "heating_time", "num_heating_stations"}
        for key, value in kwargs.items():
            if key not in skip_keys and not hasattr(self, key):
                setattr(self, key, value)

        self.logger.info(f"=== 虚拟工作台 {self.device_id} 已创建 ===")
        self.logger.info(
            f"机械臂操作时间: {self.ARM_OPERATION_TIME}s | "
            f"加热时间: {self.HEATING_TIME}s | "
            f"加热台数量: {self.NUM_HEATING_STATIONS}"
        )

    @not_action
    def post_init(self, ros_node: BaseROS2DeviceNode):
        """ROS节点初始化后回调"""
        self._ros_node = ros_node

    @not_action
    def initialize(self) -> bool:
        """初始化虚拟工作台"""
        self.logger.info(f"初始化虚拟工作台 {self.device_id}")

        # 重置加热台状态 (已在__init__中创建，这里重置为初始状态)
        with self._stations_lock:
            for station in self._heating_stations.values():
                station.state = HeatingStationState.IDLE
                station.current_material = None
                station.material_number = None
                station.heating_progress = 0.0

        # 初始化状态
        self.data.update({
            "status": "Ready",
            "arm_state": ArmState.IDLE.value,
            "arm_current_task": None,
            "heating_stations": self._get_stations_status(),
            "active_tasks_count": 0,
            "message": "工作台就绪",
        })

        self.logger.info(f"工作台初始化完成: {self.NUM_HEATING_STATIONS}个加热台就绪")
        return True

    @not_action
    def cleanup(self) -> bool:
        """清理虚拟工作台"""
        self.logger.info(f"清理虚拟工作台 {self.device_id}")

        self._arm_state = ArmState.IDLE
        self._arm_current_task = None

        with self._stations_lock:
            self._heating_stations.clear()

        with self._tasks_lock:
            self._active_tasks.clear()

        self.data.update({
            "status": "Offline",
            "arm_state": ArmState.IDLE.value,
            "heating_stations": {},
            "message": "工作台已关闭",
        })
        return True

    def _get_stations_status(self) -> Dict[int, Dict[str, Any]]:
        """获取所有加热台状态"""
        with self._stations_lock:
            return {
                station_id: {
                    "state": station.state.value,
                    "current_material": station.current_material,
                    "material_number": station.material_number,
                    "heating_progress": station.heating_progress,
                }
                for station_id, station in self._heating_stations.items()
            }

    def _update_data_status(self, message: Optional[str] = None):
        """更新状态数据"""
        self.data.update({
            "arm_state": self._arm_state.value,
            "arm_current_task": self._arm_current_task,
            "heating_stations": self._get_stations_status(),
            "active_tasks_count": len(self._active_tasks),
        })
        if message:
            self.data["message"] = message

    def _find_available_heating_station(self) -> Optional[int]:
        """查找空闲的加热台

        Returns:
            空闲加热台ID，如果没有则返回None
        """
        with self._stations_lock:
            for station_id, station in self._heating_stations.items():
                if station.state == HeatingStationState.IDLE:
                    return station_id
        return None

    def _acquire_arm(self, task_description: str) -> bool:
        """获取机械臂使用权（阻塞直到获取）

        Args:
            task_description: 任务描述，用于日志

        Returns:
            是否成功获取
        """
        self.logger.info(f"[{task_description}] 等待获取机械臂...")

        # 阻塞等待获取锁
        self._arm_lock.acquire()

        self._arm_state = ArmState.BUSY
        self._arm_current_task = task_description
        self._update_data_status(f"机械臂执行: {task_description}")

        self.logger.info(f"[{task_description}] 成功获取机械臂使用权")
        return True

    def _release_arm(self):
        """释放机械臂"""
        task = self._arm_current_task
        self._arm_state = ArmState.IDLE
        self._arm_current_task = None
        self._arm_lock.release()
        self._update_data_status(f"机械臂已释放 (完成: {task})")
        self.logger.info(f"机械臂已释放 (完成: {task})")

    def prepare_materials(
        self,
        count: int = 5,
    ) -> PrepareMaterialsResult:
        """
        批量准备物料 - 虚拟起始节点

        作为工作流的起始节点，生成指定数量的物料编号供后续节点使用。
        输出5个handle (material_1 ~ material_5)，分别对应实验1~5。

        Args:
            count: 待生成的物料数量，默认5 (生成 A1-A5)

        Returns:
            PrepareMaterialsResult: 包含 material_1 ~ material_5 用于传递给 move_to_heating_station
        """
        # 生成物料列表 A1 - A{count}
        materials = [i for i in range(1, count + 1)]

        self.logger.info(
            f"[准备物料] 生成 {count} 个物料: "
            f"A1-A{count} -> material_1~material_{count}"
        )

        return {
            "success": True,
            "count": count,
            "material_1": materials[0] if len(materials) > 0 else 0,
            "material_2": materials[1] if len(materials) > 1 else 0,
            "material_3": materials[2] if len(materials) > 2 else 0,
            "material_4": materials[3] if len(materials) > 3 else 0,
            "material_5": materials[4] if len(materials) > 4 else 0,
            "message": f"已准备 {count} 个物料: A1-A{count}",
        }

    def move_to_heating_station(
        self,
        material_number: int,
    ) -> MoveToHeatingStationResult:
        """
        将物料从An位置移动到加热台

        多线程并发调用时，会竞争机械臂使用权，并自动查找空闲加热台

        Args:
            material_number: 物料编号 (1-5)

        Returns:
            MoveToHeatingStationResult: 包含 station_id, material_number 等用于传递给下一个节点
        """
        # 根据物料编号生成物料ID
        material_id = f"A{material_number}"
        task_desc = f"移动{material_id}到加热台"
        self.logger.info(f"[任务] {task_desc} - 开始执行")

        # 记录任务
        with self._tasks_lock:
            self._active_tasks[material_id] = {
                "status": "waiting_for_arm",
                "start_time": time.time(),
            }

        try:
            # 步骤1: 等待获取机械臂使用权(竞争)
            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "waiting_for_arm"
            self._acquire_arm(task_desc)

            # 步骤2: 查找空闲加热台
            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "finding_station"
            station_id = None

            # 循环等待直到找到空闲加热台
            while station_id is None:
                station_id = self._find_available_heating_station()
                if station_id is None:
                    self.logger.info(f"[{material_id}] 没有空闲加热台，等待中...")
                    # 释放机械臂，等待后重试
                    self._release_arm()
                    time.sleep(0.5)
                    self._acquire_arm(task_desc)

            # 步骤3: 占用加热台 - 立即标记为OCCUPIED，防止其他任务选择同一加热台
            with self._stations_lock:
                self._heating_stations[station_id].state = HeatingStationState.OCCUPIED
                self._heating_stations[station_id].current_material = material_id
                self._heating_stations[station_id].material_number = material_number

            # 步骤4: 模拟机械臂移动操作 (3秒)
            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "arm_moving"
                self._active_tasks[material_id]["assigned_station"] = station_id
            self.logger.info(f"[{material_id}] 机械臂正在移动到加热台{station_id}...")

            time.sleep(self.ARM_OPERATION_TIME)

            # 步骤5: 放入加热台完成
            self._update_data_status(f"{material_id}已放入加热台{station_id}")
            self.logger.info(f"[{material_id}] 已放入加热台{station_id} (用时{self.ARM_OPERATION_TIME}s)")

            # 释放机械臂
            self._release_arm()

            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "placed_on_station"

            return {
                "success": True,
                "station_id": station_id,
                "material_id": material_id,
                "material_number": material_number,
                "message": f"{material_id}已成功移动到加热台{station_id}",
            }

        except Exception as e:
            self.logger.error(f"[{material_id}] 移动失败: {str(e)}")
            if self._arm_lock.locked():
                self._release_arm()
            return {
                "success": False,
                "station_id": -1,
                "material_id": material_id,
                "material_number": material_number,
                "message": f"移动失败: {str(e)}",
            }

    def start_heating(
        self,
        station_id: int,
        material_number: int,
    ) -> StartHeatingResult:
        """
        启动指定加热台的加热程序

        Args:
            station_id: 加热台ID (1-3)，从 move_to_heating_station 的 handle 传入
            material_number: 物料编号，从 move_to_heating_station 的 handle 传入

        Returns:
            StartHeatingResult: 包含 station_id, material_number 等用于传递给下一个节点
        """
        self.logger.info(f"[加热台{station_id}] 开始加热")

        if station_id not in self._heating_stations:
            return {
                "success": False,
                "station_id": station_id,
                "material_id": "",
                "material_number": material_number,
                "message": f"无效的加热台ID: {station_id}",
            }

        with self._stations_lock:
            station = self._heating_stations[station_id]

            if station.current_material is None:
                return {
                    "success": False,
                    "station_id": station_id,
                    "material_id": "",
                    "material_number": material_number,
                    "message": f"加热台{station_id}上没有物料",
                }

            if station.state == HeatingStationState.HEATING:
                return {
                    "success": False,
                    "station_id": station_id,
                    "material_id": station.current_material,
                    "material_number": material_number,
                    "message": f"加热台{station_id}已经在加热中",
                }

            material_id = station.current_material

            # 开始加热
            station.state = HeatingStationState.HEATING
            station.heating_start_time = time.time()
            station.heating_progress = 0.0

        with self._tasks_lock:
            if material_id in self._active_tasks:
                self._active_tasks[material_id]["status"] = "heating"

        self._update_data_status(f"加热台{station_id}开始加热{material_id}")

        # 模拟加热过程 (10秒)
        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            progress = min(100.0, (elapsed / self.HEATING_TIME) * 100)

            with self._stations_lock:
                self._heating_stations[station_id].heating_progress = progress

            self._update_data_status(f"加热台{station_id}加热中: {progress:.1f}%")

            if elapsed >= self.HEATING_TIME:
                break

            time.sleep(1.0)

        # 加热完成
        with self._stations_lock:
            self._heating_stations[station_id].state = HeatingStationState.COMPLETED
            self._heating_stations[station_id].heating_progress = 100.0

        with self._tasks_lock:
            if material_id in self._active_tasks:
                self._active_tasks[material_id]["status"] = "heating_completed"

        self._update_data_status(f"加热台{station_id}加热完成")
        self.logger.info(f"[加热台{station_id}] {material_id}加热完成 (用时{self.HEATING_TIME}s)")

        return {
            "success": True,
            "station_id": station_id,
            "material_id": material_id,
            "material_number": material_number,
            "message": f"加热台{station_id}加热完成",
        }

    def move_to_output(
        self,
        station_id: int,
        material_number: int,
    ) -> MoveToOutputResult:
        """
        将物料从加热台移动到输出位置Cn

        Args:
            station_id: 加热台ID (1-3)，从 start_heating 的 handle 传入
            material_number: 物料编号，从 start_heating 的 handle 传入，用于确定输出位置 Cn

        Returns:
            MoveToOutputResult: 包含执行结果
        """
        output_number = material_number  # 物料编号决定输出位置

        if station_id not in self._heating_stations:
            return {
                "success": False,
                "station_id": station_id,
                "material_id": "",
                "output_position": f"C{output_number}",
                "message": f"无效的加热台ID: {station_id}",
            }

        with self._stations_lock:
            station = self._heating_stations[station_id]
            material_id = station.current_material

            if material_id is None:
                return {
                    "success": False,
                    "station_id": station_id,
                    "material_id": "",
                    "output_position": f"C{output_number}",
                    "message": f"加热台{station_id}上没有物料",
                }

            if station.state != HeatingStationState.COMPLETED:
                return {
                    "success": False,
                    "station_id": station_id,
                    "material_id": material_id,
                    "output_position": f"C{output_number}",
                    "message": f"加热台{station_id}尚未完成加热 (当前状态: {station.state.value})",
                }

        output_position = f"C{output_number}"
        task_desc = f"从加热台{station_id}移动{material_id}到{output_position}"
        self.logger.info(f"[任务] {task_desc}")

        try:
            with self._tasks_lock:
                if material_id in self._active_tasks:
                    self._active_tasks[material_id]["status"] = "waiting_for_arm_output"

            # 获取机械臂
            self._acquire_arm(task_desc)

            with self._tasks_lock:
                if material_id in self._active_tasks:
                    self._active_tasks[material_id]["status"] = "arm_moving_to_output"

            # 模拟机械臂操作 (3秒)
            self.logger.info(f"[{material_id}] 机械臂正在从加热台{station_id}取出并移动到{output_position}...")
            time.sleep(self.ARM_OPERATION_TIME)

            # 清空加热台
            with self._stations_lock:
                self._heating_stations[station_id].state = HeatingStationState.IDLE
                self._heating_stations[station_id].current_material = None
                self._heating_stations[station_id].material_number = None
                self._heating_stations[station_id].heating_progress = 0.0
                self._heating_stations[station_id].heating_start_time = None

            # 释放机械臂
            self._release_arm()

            # 任务完成
            with self._tasks_lock:
                if material_id in self._active_tasks:
                    self._active_tasks[material_id]["status"] = "completed"
                    self._active_tasks[material_id]["end_time"] = time.time()

            self._update_data_status(f"{material_id}已移动到{output_position}")
            self.logger.info(f"[{material_id}] 已成功移动到{output_position} (用时{self.ARM_OPERATION_TIME}s)")

            return {
                "success": True,
                "station_id": station_id,
                "material_id": material_id,
                "output_position": output_position,
                "message": f"{material_id}已成功移动到{output_position}",
            }

        except Exception as e:
            self.logger.error(f"移动到输出位置失败: {str(e)}")
            if self._arm_lock.locked():
                self._release_arm()
            return {
                "success": False,
                "station_id": station_id,
                "material_id": "",
                "output_position": output_position,
                "message": f"移动失败: {str(e)}",
            }

    # ============ 状态属性 ============

    @property
    def status(self) -> str:
        return self.data.get("status", "Unknown")

    @property
    def arm_state(self) -> str:
        return self._arm_state.value

    @property
    def arm_current_task(self) -> str:
        return self._arm_current_task or ""

    @property
    def heating_station_1_state(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(1)
            return station.state.value if station else "unknown"

    @property
    def heating_station_1_material(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(1)
            return station.current_material or "" if station else ""

    @property
    def heating_station_1_progress(self) -> float:
        with self._stations_lock:
            station = self._heating_stations.get(1)
            return station.heating_progress if station else 0.0

    @property
    def heating_station_2_state(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(2)
            return station.state.value if station else "unknown"

    @property
    def heating_station_2_material(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(2)
            return station.current_material or "" if station else ""

    @property
    def heating_station_2_progress(self) -> float:
        with self._stations_lock:
            station = self._heating_stations.get(2)
            return station.heating_progress if station else 0.0

    @property
    def heating_station_3_state(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(3)
            return station.state.value if station else "unknown"

    @property
    def heating_station_3_material(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(3)
            return station.current_material or "" if station else ""

    @property
    def heating_station_3_progress(self) -> float:
        with self._stations_lock:
            station = self._heating_stations.get(3)
            return station.heating_progress if station else 0.0

    @property
    def active_tasks_count(self) -> int:
        with self._tasks_lock:
            return len(self._active_tasks)

    @property
    def message(self) -> str:
        return self.data.get("message", "")
