"""
Virtual Workbench Device - 模拟工作台设备
包含:
- 1个机械臂 (每次操作3s, 独占锁)
- 3个加热台 (每次加热10s, 可并行)

工作流程:
1. A1-A5 物料同时启动, 竞争机械臂
2. 机械臂将物料移动到空闲加热台
3. 加热完成后, 机械臂将物料移动到C1-C5

注意: 调用来自线程池, 使用 threading.Lock 进行同步
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock, RLock
from typing import Any, Dict, List, Optional, cast

from typing_extensions import TypedDict

from unilabos.registry.decorators import (
    ActionInputHandle,
    ActionOutputHandle,
    DataSource,
    NodeType,
    action,
    device,
    not_action,
    topic_config,
)
from unilabos.device_runtime.node import DeviceNode
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from unilabos.resources.resource_tracker import (
    ResourceTreeSet,
)
from unilabos.resources.objects.sample import LabSample, SampleUUIDsType
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
    ResourceDictPositionSize,
)
from unilabos.resources.objects.site import SiteDefinition

# ============ TypedDict 返回类型定义 ============


class MoveToHeatingStationResult(TypedDict):
    """move_to_heating_station 返回类型"""

    success: bool
    station_id: int
    material_id: str
    material_number: int
    message: str
    unilabos_samples: List[LabSample]


class StartHeatingResult(TypedDict):
    """start_heating 返回类型"""

    success: bool
    station_id: int
    material_id: str
    material_number: int
    message: str
    unilabos_samples: List[LabSample]


class MoveToOutputResult(TypedDict):
    """move_to_output 返回类型"""

    success: bool
    station_id: int
    material_id: str
    output_position: str
    message: str
    unilabos_samples: List[LabSample]


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
    unilabos_samples: List[LabSample]


# ============ 状态枚举 ============


class HeatingStationState(Enum):
    """加热台状态枚举"""

    IDLE = "idle"  # 空闲
    OCCUPIED = "occupied"  # 已放置物料, 等待加热
    HEATING = "heating"  # 加热中
    COMPLETED = "completed"  # 加热完成, 等待取走


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


VIRTUAL_WORKBENCH_AVAILABLE_SITES: List[SiteDefinition] = [
    SiteDefinition(
        index=1,
        label="heating_station_1",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=100.0, y=100.0, z=0.0),
            position3d=ResourceDictPositionObject(x=100.0, y=100.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["workbench_material"],
        parent_link="heating_station_1",
        description="虚拟工作台加热位 1",
        meta_data={"station_id": 1, "role": "heating"},
    ),
    SiteDefinition(
        index=2,
        label="heating_station_2",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=250.0, y=100.0, z=0.0),
            position3d=ResourceDictPositionObject(x=250.0, y=100.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["workbench_material"],
        parent_link="heating_station_2",
        description="虚拟工作台加热位 2",
        meta_data={"station_id": 2, "role": "heating"},
    ),
    SiteDefinition(
        index=3,
        label="heating_station_3",
        pose=ResourceDictPosition(
            position=ResourceDictPositionObject(x=400.0, y=100.0, z=0.0),
            position3d=ResourceDictPositionObject(x=400.0, y=100.0, z=0.0),
            size=ResourceDictPositionSize(width=100.0, height=100.0, depth=20.0),
        ),
        allowed_resource_categories=["workbench_material"],
        parent_link="heating_station_3",
        description="虚拟工作台加热位 3",
        meta_data={"station_id": 3, "role": "heating"},
    ),
]


@device(
    id="virtual_workbench",
    displayname="虚拟工作台",
    category=["virtual_device"],
    description="Virtual Workbench with 1 robotic arm and 3 heating stations for concurrent material processing",
    available_sites=VIRTUAL_WORKBENCH_AVAILABLE_SITES,
    supported_backends=["ros2"],
)
class VirtualWorkbench:
    """
    Virtual Workbench Device - 虚拟工作台设备

    模拟一个包含1个机械臂和3个加热台的工作站
    - 机械臂操作耗时3秒, 同一时间只能执行一个操作
    - 加热台加热耗时10秒, 3个加热台可并行工作

    工作流:
    1. 物料A1-A5并发启动(线程池), 竞争机械臂使用权
    2. 获取机械臂后, 查找空闲加热台
    3. 机械臂将物料放入加热台, 开始加热
    4. 加热完成后, 机械臂将物料移动到目标位置Cn
    """

    _ros_node: DeviceNode

    # 配置常量
    ARM_OPERATION_TIME: float = 2  # 机械臂操作时间(秒)
    HEATING_TIME: float = 60.0  # 加热时间(秒)
    NUM_HEATING_STATIONS: int = 3  # 加热台数量

    def __init__(
        self,
        device_id: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """
        初始化虚拟工作台。

        Args:
            device_id[设备ID]: 工作台设备实例 ID，默认使用 virtual_workbench。
            config[设备配置]: 可包含 arm_operation_time、heating_time、num_heating_stations。
        """
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
        self.ARM_OPERATION_TIME = float(
            self.config.get("arm_operation_time", self.ARM_OPERATION_TIME)
        )
        self.HEATING_TIME = float(self.config.get("heating_time", self.HEATING_TIME))
        self.NUM_HEATING_STATIONS = int(
            self.config.get("num_heating_stations", self.NUM_HEATING_STATIONS)
        )

        # 机械臂状态和锁
        self._arm_lock = Lock()
        self._arm_state = ArmState.IDLE
        self._arm_current_task: Optional[str] = None

        # 加热台状态
        self._heating_stations: Dict[int, HeatingStation] = {
            i: HeatingStation(station_id=i)
            for i in range(1, self.NUM_HEATING_STATIONS + 1)
        }
        self._stations_lock = RLock()

        # 任务追踪
        self._active_tasks: Dict[str, Dict[str, Any]] = {}
        self._tasks_lock = Lock()

        # 本地订阅演示: 自增计数器与其派生状态
        self._start_time: float = time.time()

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
    def post_init(self, ros_node: DeviceNode):
        """ROS节点初始化后回调"""
        self._ros_node = ros_node

    @not_action
    def initialize(self) -> bool:
        """初始化虚拟工作台"""
        self.logger.info(f"初始化虚拟工作台 {self.device_id}")

        with self._stations_lock:
            for station in self._heating_stations.values():
                station.state = HeatingStationState.IDLE
                station.current_material = None
                station.material_number = None
                station.heating_progress = 0.0

        self.data.update(
            {
                "status": "Ready",
                "arm_state": ArmState.IDLE.value,
                "arm_current_task": None,
                "heating_stations": self._get_stations_status(),
                "active_tasks_count": 0,
                "message": "工作台就绪",
            }
        )

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

        self.data.update(
            {
                "status": "Offline",
                "arm_state": ArmState.IDLE.value,
                "heating_stations": {},
                "message": "工作台已关闭",
            }
        )
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
        self.data.update(
            {
                "arm_state": self._arm_state.value,
                "arm_current_task": self._arm_current_task,
                "heating_stations": self._get_stations_status(),
                "active_tasks_count": len(self._active_tasks),
            }
        )
        if message:
            self.data["message"] = message

    def _find_available_heating_station(self) -> Optional[int]:
        """查找空闲的加热台"""
        with self._stations_lock:
            for station_id, station in self._heating_stations.items():
                if station.state == HeatingStationState.IDLE:
                    return station_id
        return None

    def _acquire_arm(self, task_description: str) -> bool:
        """获取机械臂使用权(阻塞直到获取)"""
        self.logger.info(f"[{task_description}] 等待获取机械臂...")
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

    # ============ 本地派生状态演示: 直接用 getter 计算 ============

    @property
    @topic_config(period=1.0)
    def counter(self) -> int:
        """实时增长的计数器(自启动起的秒数)，每秒发布到 /devices/<device_id>/counter。"""
        return int(time.time() - self._start_time)

    @property
    @topic_config(period=1.0)
    def counter_echo(self) -> int:
        """counter 的派生状态 (= counter * 10)。本设备自己的派生态直接用 getter 计算即可，
        无需自订阅自己的 topic（@subscribe 仅用于跨设备订阅）。"""
        return int(time.time() - self._start_time) * 10

    @action(description="跨设备调用演示: 调用目标设备的某个函数并返回其结果")
    def call_peer(
        self,
        target_device: str,
        function_name: str,
        function_args: str = "{}",
    ) -> dict:
        """
        演示通过 _ros_node 便捷函数跨设备调用动作（走 serial JSON 指令通道）。

        Args:
            target_device[目标设备]: 被调用设备的 ID（可带或不带 /devices/ 前缀）。
            function_name[函数名]: 目标设备上要调用的函数 / 动作名。
            function_args[入参JSON]: 入参，UI 传来的 JSON 字符串；本动作 json.loads 成 dict 后传给 call_device_action。

        Note:
            远端执行失败会以 DeviceActionError 在此处 raise，从而让本动作整体失败。
        """
        # call_device_action 只接受 dict 入参（序列化由其内部完成）；UI 传来的是 JSON 字符串，这里先解析成 dict
        kwargs = json.loads(function_args) if function_args else {}
        # 同步 action 在线程池中执行，使用同步便捷函数即可（阻塞安全）
        return_value = self._ros_node.call_device_action(target_device, function_name, kwargs)
        return {"success": True, "target_device": target_device, "function_name": function_name, "return_value": return_value}

    @action(
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={"assignee_user_ids": "unilabos_manual_confirm"},
        goal_default={"timeout_seconds": 3600, "assignee_user_ids": []},
        feedback_interval=300,
        handles=[
            ActionInputHandle(
                key="target_device",
                data_type="device_id",
                label="目标设备",
                data_key="target_device",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="待转移资源",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="mount_resource",
                data_type="resource",
                label="目标孔位",
                data_key="mount_resource",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="collector_mass",
                data_type="collector_mass",
                label="极流体质量",
                data_key="collector_mass",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="active_material",
                data_type="active_material",
                label="活性物质含量",
                data_key="active_material",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="capacity",
                data_type="capacity",
                label="克容量",
                data_key="capacity",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="battery_system",
                data_type="battery_system",
                label="电池体系",
                data_key="battery_system",
                data_source=DataSource.HANDLE,
            ),
            # transfer使用
            ActionOutputHandle(
                key="target_device",
                data_type="device_id",
                label="目标设备",
                data_key="target_device",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="resource",
                data_type="resource",
                label="待转移资源",
                data_key="resource.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="mount_resource",
                data_type="resource",
                label="目标孔位",
                data_key="mount_resource.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
            # test使用
            ActionOutputHandle(
                key="collector_mass",
                data_type="collector_mass",
                label="极流体质量",
                data_key="collector_mass",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="active_material",
                data_type="active_material",
                label="活性物质含量",
                data_key="active_material",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="capacity",
                data_type="capacity",
                label="克容量",
                data_key="capacity",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="battery_system",
                data_type="battery_system",
                label="电池体系",
                data_key="battery_system",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def manual_confirm(
        self,
        resource: List[ResourceSlot],
        target_device: DeviceSlot,
        mount_resource: List[ResourceSlot],
        collector_mass: List[float],
        active_material: List[float],
        capacity: List[float],
        battery_system: List[str],
        timeout_seconds: int,
        assignee_user_ids: list[str],
        **kwargs,
    ) -> dict:
        """
        人工确认资源转移和扣电测试参数。

        Args:
            resource[待转移资源]: 需要人工确认的资源列表。
            target_device[目标设备]: 资源要转移到的目标设备 ID。
            mount_resource[目标孔位]: 资源要挂载到的目标孔位列表。
            collector_mass[极流体质量]: 每个样品对应的极流体质量。
            active_material[活性物质含量]: 每个样品对应的活性物质含量。
            capacity[克容量]: 每个样品对应的克容量，单位 mAh/g。
            battery_system[电池体系]: 每个样品对应的电池体系名称。
            timeout_seconds[超时时间]: 人工确认超时时间，单位秒。
            assignee_user_ids[确认人]: 指定处理人工确认任务的用户 ID 列表。

        Note:
            修改的结果无效，是只读的。
        """
        resource_tree = ResourceTreeSet.from_plr_resources(cast(Any, resource)).dump()
        mount_resource_tree = ResourceTreeSet.from_plr_resources(cast(Any, mount_resource)).dump()
        kwargs.update(locals())
        kwargs.pop("kwargs")
        kwargs.pop("self")
        kwargs["resource"] = resource_tree
        kwargs["mount_resource"] = mount_resource_tree
        kwargs.pop("resource_tree")
        kwargs.pop("mount_resource_tree")
        return kwargs

    @action(
        description="转移物料",
        handles=[
            ActionInputHandle(
                key="target_device",
                data_type="device_id",
                label="目标设备",
                data_key="target_device",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="待转移资源",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="mount_resource",
                data_type="resource",
                label="目标孔位",
                data_key="mount_resource",
                data_source=DataSource.HANDLE,
            ),
        ],
    )
    async def transfer(
        self,
        resource: List[ResourceSlot],
        target_device: DeviceSlot,
        mount_resource: List[ResourceSlot],
    ):
        """
        转移资源到目标设备。

        Args:
            resource[待转移资源]: 待转移的资源列表。
            target_device[目标设备]: 接收资源的目标设备 ID。
            mount_resource[目标孔位]: 目标设备上的挂载孔位列表。
        """
        return await self._ros_node.transfer_resource_to_another(
            plr_resources=resource,
            target_device_id=target_device,
            target_resources=mount_resource,
            sites=[None] * len(mount_resource),
        )

    @action(
        description="扣电测试启动",
        handles=[
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="待转移资源",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="mount_resource",
                data_type="resource",
                label="目标孔位",
                data_key="mount_resource",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="collector_mass",
                data_type="collector_mass",
                label="极流体质量",
                data_key="collector_mass",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="active_material",
                data_type="active_material",
                label="活性物质含量",
                data_key="active_material",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="capacity",
                data_type="capacity",
                label="克容量",
                data_key="capacity",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="battery_system",
                data_type="battery_system",
                label="电池体系",
                data_key="battery_system",
                data_source=DataSource.HANDLE,
            ),
        ],
    )
    async def test(
        self,
        resource: List[ResourceSlot],
        mount_resource: List[ResourceSlot],
        collector_mass: List[float],
        active_material: List[float],
        capacity: List[float],
        battery_system: list[str],
    ):
        """
        启动扣电测试。

        Args:
            resource[待测试资源]: 需要进行扣电测试的资源列表。
            mount_resource[测试孔位]: 扣电测试使用的目标孔位列表。
            collector_mass[极流体质量]: 每个样品对应的极流体质量。
            active_material[活性物质含量]: 每个样品对应的活性物质含量。
            capacity[克容量]: 每个样品对应的克容量，单位 mAh/g。
            battery_system[电池体系]: 每个样品对应的电池体系名称。
        """
        print(resource)
        print(mount_resource)
        print(collector_mass)
        print(active_material)
        print(capacity)
        print(battery_system)

    @action(
        auto_prefix=True,
        description="批量准备物料 - 虚拟起始节点, 生成A1-A5物料, 输出5个handle供后续节点使用",
        handles=[
            ActionOutputHandle(key="channel_1", data_type="workbench_material", label="实验1", data_key="material_1", data_source=DataSource.EXECUTOR),  # noqa: E501
            ActionOutputHandle(key="channel_2", data_type="workbench_material", label="实验2", data_key="material_2", data_source=DataSource.EXECUTOR),  # noqa: E501
            ActionOutputHandle(key="channel_3", data_type="workbench_material", label="实验3", data_key="material_3", data_source=DataSource.EXECUTOR),  # noqa: E501
            ActionOutputHandle(key="channel_4", data_type="workbench_material", label="实验4", data_key="material_4", data_source=DataSource.EXECUTOR),  # noqa: E501
            ActionOutputHandle(key="channel_5", data_type="workbench_material", label="实验5", data_key="material_5", data_source=DataSource.EXECUTOR),  # noqa: E501
        ],
    )
    def prepare_materials(
        self,
        sample_uuids: SampleUUIDsType,
        count: int = 5,
    ) -> PrepareMaterialsResult:
        """
        批量准备物料 - 虚拟起始节点

        作为工作流的起始节点, 生成指定数量的物料编号供后续节点使用。
        输出5个handle (material_1 ~ material_5), 分别对应实验1~5。

        Args:
            count[物料数量]: 要生成的物料数量，默认生成 5 个。
        """
        materials = [i for i in range(1, count + 1)]

        self.logger.info(
            f"[准备物料] 生成 {count} 个物料: A1-A{count} -> material_1~material_{count}"
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
            "unilabos_samples": [
                LabSample(
                    sample_uuid=sample_uuid,
                    oss_path="",
                    extra=(
                        {"material_uuid": content}
                        if isinstance(content, str)
                        else (content.serialize() if content else {})
                    ),
                )
                for sample_uuid, content in sample_uuids.items()
            ],
        }

    @action(
        auto_prefix=True,
        description="将物料从An位置移动到空闲加热台, 返回分配的加热台ID",
        handles=[
            ActionInputHandle(
                key="material_input",
                data_type="workbench_material",
                label="物料编号",
                data_key="material_number",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="heating_station_output",
                data_type="workbench_station",
                label="加热台ID",
                data_key="station_id",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="material_number_output",
                data_type="workbench_material",
                label="物料编号",
                data_key="material_number",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def move_to_heating_station(
        self,
        sample_uuids: SampleUUIDsType,
        material_number: int,
    ) -> MoveToHeatingStationResult:
        """
        将物料从An位置移动到加热台

        多线程并发调用时, 会竞争机械臂使用权, 并自动查找空闲加热台

        Args:
            material_number[物料编号]: 要移动的物料编号，对应 A1、A2 等起始位置。
        """
        material_id = f"A{material_number}"
        task_desc = f"移动{material_id}到加热台"
        self.logger.info(f"[任务] {task_desc} - 开始执行")

        with self._tasks_lock:
            self._active_tasks[material_id] = {
                "status": "waiting_for_arm",
                "start_time": time.time(),
            }

        try:
            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "waiting_for_arm"
            self._acquire_arm(task_desc)

            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "finding_station"
            station_id = None

            while station_id is None:
                station_id = self._find_available_heating_station()
                if station_id is None:
                    self.logger.info(f"[{material_id}] 没有空闲加热台, 等待中...")
                    self._release_arm()
                    time.sleep(0.5)
                    self._acquire_arm(task_desc)

            with self._stations_lock:
                self._heating_stations[station_id].state = HeatingStationState.OCCUPIED
                self._heating_stations[station_id].current_material = material_id
                self._heating_stations[station_id].material_number = material_number

            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "arm_moving"
                self._active_tasks[material_id]["assigned_station"] = station_id
            self.logger.info(f"[{material_id}] 机械臂正在移动到加热台{station_id}...")

            time.sleep(self.ARM_OPERATION_TIME)

            self._update_data_status(f"{material_id}已放入加热台{station_id}")
            self.logger.info(
                f"[{material_id}] 已放入加热台{station_id} (用时{self.ARM_OPERATION_TIME}s)"
            )

            self._release_arm()

            with self._tasks_lock:
                self._active_tasks[material_id]["status"] = "placed_on_station"

            return {
                "success": True,
                "station_id": station_id,
                "material_id": material_id,
                "material_number": material_number,
                "message": f"{material_id}已成功移动到加热台{station_id}",
                "unilabos_samples": [
                    LabSample(
                        sample_uuid=sample_uuid,
                        oss_path="",
                        extra=(
                            {"material_uuid": content}
                            if isinstance(content, str)
                            else (content.serialize() if content else {})
                        ),
                    )
                    for sample_uuid, content in sample_uuids.items()
                ],
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
                "unilabos_samples": [
                    LabSample(
                        sample_uuid=sample_uuid,
                        oss_path="",
                        extra=(
                            {"material_uuid": content}
                            if isinstance(content, str)
                            else (content.serialize() if content else {})
                        ),
                    )
                    for sample_uuid, content in sample_uuids.items()
                ],
            }

    @action(
        auto_prefix=True,
        always_free=True,
        description="启动指定加热台的加热程序",
        handles=[
            ActionInputHandle(
                key="station_id_input",
                data_type="workbench_station",
                label="加热台ID",
                data_key="station_id",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="material_number_input",
                data_type="workbench_material",
                label="物料编号",
                data_key="material_number",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="heating_done_station",
                data_type="workbench_station",
                label="加热完成-加热台ID",
                data_key="station_id",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="heating_done_material",
                data_type="workbench_material",
                label="加热完成-物料编号",
                data_key="material_number",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    def start_heating(
        self,
        sample_uuids: SampleUUIDsType,
        station_id: int,
        material_number: int,
    ) -> StartHeatingResult:
        """
        启动指定加热台的加热程序

        Args:
            station_id[加热台ID]: 要启动加热的加热台编号。
            material_number[物料编号]: 当前加热台上的物料编号。
        """
        self.logger.info(f"[加热台{station_id}] 开始加热")

        if station_id not in self._heating_stations:
            return {
                "success": False,
                "station_id": station_id,
                "material_id": "",
                "material_number": material_number,
                "message": f"无效的加热台ID: {station_id}",
                "unilabos_samples": [
                    LabSample(
                        sample_uuid=sample_uuid,
                        oss_path="",
                        extra=(
                            {"material_uuid": content}
                            if isinstance(content, str)
                            else (content.serialize() if content else {})
                        ),
                    )
                    for sample_uuid, content in sample_uuids.items()
                ],
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
                    "unilabos_samples": [
                        LabSample(
                            sample_uuid=sample_uuid,
                            oss_path="",
                            extra=(
                                {"material_uuid": content}
                                if isinstance(content, str)
                                else (content.serialize() if content else {})
                            ),
                        )
                        for sample_uuid, content in sample_uuids.items()
                    ],
                }

            if station.state == HeatingStationState.HEATING:
                return {
                    "success": False,
                    "station_id": station_id,
                    "material_id": station.current_material,
                    "material_number": material_number,
                    "message": f"加热台{station_id}已经在加热中",
                    "unilabos_samples": [
                        LabSample(
                            sample_uuid=sample_uuid,
                            oss_path="",
                            extra=(
                                {"material_uuid": content}
                                if isinstance(content, str)
                                else (content.serialize() if content else {})
                            ),
                        )
                        for sample_uuid, content in sample_uuids.items()
                    ],
                }

            material_id = station.current_material

            station.state = HeatingStationState.HEATING
            station.heating_start_time = time.time()
            station.heating_progress = 0.0

        with self._tasks_lock:
            if material_id in self._active_tasks:
                self._active_tasks[material_id]["status"] = "heating"

        self._update_data_status(f"加热台{station_id}开始加热{material_id}")

        with self._stations_lock:
            heating_list = [
                f"加热台{sid}:{s.current_material}"
                for sid, s in self._heating_stations.items()
                if s.state == HeatingStationState.HEATING and s.current_material
            ]
        self.logger.info(f"[并行加热] 当前同时加热中: {', '.join(heating_list)}")

        start_time = time.time()
        last_countdown_log = start_time
        while True:
            elapsed = time.time() - start_time
            remaining = max(0.0, self.HEATING_TIME - elapsed)
            progress = min(100.0, (elapsed / self.HEATING_TIME) * 100)

            with self._stations_lock:
                self._heating_stations[station_id].heating_progress = progress

            self._update_data_status(f"加热台{station_id}加热中: {progress:.1f}%")

            if time.time() - last_countdown_log >= 5.0:
                self.logger.info(
                    f"[加热台{station_id}] {material_id} 剩余 {remaining:.1f}s"
                )
                last_countdown_log = time.time()

            if elapsed >= self.HEATING_TIME:
                break

            time.sleep(1.0)

        with self._stations_lock:
            self._heating_stations[station_id].state = HeatingStationState.COMPLETED
            self._heating_stations[station_id].heating_progress = 100.0

        with self._tasks_lock:
            if material_id in self._active_tasks:
                self._active_tasks[material_id]["status"] = "heating_completed"

        self._update_data_status(f"加热台{station_id}加热完成")
        self.logger.info(
            f"[加热台{station_id}] {material_id}加热完成 (用时{self.HEATING_TIME}s)"
        )

        return {
            "success": True,
            "station_id": station_id,
            "material_id": material_id,
            "material_number": material_number,
            "message": f"加热台{station_id}加热完成",
            "unilabos_samples": [
                LabSample(
                    sample_uuid=sample_uuid,
                    oss_path="",
                    extra=(
                        {"material_uuid": content}
                        if isinstance(content, str)
                        else (content.serialize() if content else {})
                    ),
                )
                for sample_uuid, content in sample_uuids.items()
            ],
        }

    @action(
        auto_prefix=True,
        description="将物料从加热台移动到输出位置Cn",
        handles=[
            ActionInputHandle(
                key="output_station_input",
                data_type="workbench_station",
                label="加热台ID",
                data_key="station_id",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="output_material_input",
                data_type="workbench_material",
                label="物料编号",
                data_key="material_number",
                data_source=DataSource.HANDLE,
            ),
        ],
    )
    def move_to_output(
        self,
        sample_uuids: SampleUUIDsType,
        station_id: int,
        material_number: int,
    ) -> MoveToOutputResult:
        """
        将物料从加热台移动到输出位置Cn

        Args:
            station_id[加热台ID]: 已完成加热的加热台编号。
            material_number[物料编号]: 要移动到输出位置的物料编号，对应 Cn。
        """
        output_number = material_number

        if station_id not in self._heating_stations:
            return {
                "success": False,
                "station_id": station_id,
                "material_id": "",
                "output_position": f"C{output_number}",
                "message": f"无效的加热台ID: {station_id}",
                "unilabos_samples": [
                    LabSample(
                        sample_uuid=sample_uuid,
                        oss_path="",
                        extra=(
                            {"material_uuid": content}
                            if isinstance(content, str)
                            else (content.serialize() if content else {})
                        ),
                    )
                    for sample_uuid, content in sample_uuids.items()
                ],
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
                    "unilabos_samples": [
                        LabSample(
                            sample_uuid=sample_uuid,
                            oss_path="",
                            extra=(
                                {"material_uuid": content}
                                if isinstance(content, str)
                                else (content.serialize() if content else {})
                            ),
                        )
                        for sample_uuid, content in sample_uuids.items()
                    ],
                }

            if station.state != HeatingStationState.COMPLETED:
                return {
                    "success": False,
                    "station_id": station_id,
                    "material_id": material_id,
                    "output_position": f"C{output_number}",
                    "message": f"加热台{station_id}尚未完成加热 (当前状态: {station.state.value})",
                    "unilabos_samples": [
                        LabSample(
                            sample_uuid=sample_uuid,
                            oss_path="",
                            extra=(
                                {"material_uuid": content}
                                if isinstance(content, str)
                                else (content.serialize() if content else {})
                            ),
                        )
                        for sample_uuid, content in sample_uuids.items()
                    ],
                }

        output_position = f"C{output_number}"
        task_desc = f"从加热台{station_id}移动{material_id}到{output_position}"
        self.logger.info(f"[任务] {task_desc}")

        try:
            with self._tasks_lock:
                if material_id in self._active_tasks:
                    self._active_tasks[material_id]["status"] = "waiting_for_arm_output"

            self._acquire_arm(task_desc)

            with self._tasks_lock:
                if material_id in self._active_tasks:
                    self._active_tasks[material_id]["status"] = "arm_moving_to_output"

            self.logger.info(
                f"[{material_id}] 机械臂正在从加热台{station_id}取出并移动到{output_position}..."
            )
            time.sleep(self.ARM_OPERATION_TIME)

            with self._stations_lock:
                self._heating_stations[station_id].state = HeatingStationState.IDLE
                self._heating_stations[station_id].current_material = None
                self._heating_stations[station_id].material_number = None
                self._heating_stations[station_id].heating_progress = 0.0
                self._heating_stations[station_id].heating_start_time = None

            self._release_arm()

            with self._tasks_lock:
                if material_id in self._active_tasks:
                    self._active_tasks[material_id]["status"] = "completed"
                    self._active_tasks[material_id]["end_time"] = time.time()

            self._update_data_status(f"{material_id}已移动到{output_position}")
            self.logger.info(
                f"[{material_id}] 已成功移动到{output_position} (用时{self.ARM_OPERATION_TIME}s)"
            )

            return {
                "success": True,
                "station_id": station_id,
                "material_id": material_id,
                "output_position": output_position,
                "message": f"{material_id}已成功移动到{output_position}",
                "unilabos_samples": [
                    LabSample(
                        sample_uuid=sample_uuid,
                        oss_path="",
                        extra=(
                            {"material_uuid": content}
                            if isinstance(content, str)
                            else (content.serialize() if content is not None else {})
                        ),
                    )
                    for sample_uuid, content in sample_uuids.items()
                ],
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
                "unilabos_samples": [
                    LabSample(
                        sample_uuid=sample_uuid,
                        oss_path="",
                        extra=(
                            {"material_uuid": content}
                            if isinstance(content, str)
                            else (content.serialize() if content else {})
                        ),
                    )
                    for sample_uuid, content in sample_uuids.items()
                ],
            }

    # ============ 状态属性 ============

    @property
    @topic_config()
    def status(self) -> str:
        return self.data.get("status", "Unknown")

    @property
    @topic_config()
    def arm_state(self) -> str:
        return self._arm_state.value

    @property
    @topic_config()
    def arm_current_task(self) -> str:
        return self._arm_current_task or ""

    @property
    @topic_config()
    def heating_station_1_state(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(1)
            return station.state.value if station else "unknown"

    @property
    @topic_config()
    def heating_station_1_material(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(1)
            return station.current_material or "" if station else ""

    @property
    @topic_config()
    def heating_station_1_progress(self) -> float:
        with self._stations_lock:
            station = self._heating_stations.get(1)
            return station.heating_progress if station else 0.0

    @property
    @topic_config()
    def heating_station_2_state(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(2)
            return station.state.value if station else "unknown"

    @property
    @topic_config()
    def heating_station_2_material(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(2)
            return station.current_material or "" if station else ""

    @property
    @topic_config()
    def heating_station_2_progress(self) -> float:
        with self._stations_lock:
            station = self._heating_stations.get(2)
            return station.heating_progress if station else 0.0

    @property
    @topic_config()
    def heating_station_3_state(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(3)
            return station.state.value if station else "unknown"

    @property
    @topic_config()
    def heating_station_3_material(self) -> str:
        with self._stations_lock:
            station = self._heating_stations.get(3)
            return station.current_material or "" if station else ""

    @property
    @topic_config()
    def heating_station_3_progress(self) -> float:
        with self._stations_lock:
            station = self._heating_stations.get(3)
            return station.heating_progress if station else 0.0

    @property
    @topic_config()
    def active_tasks_count(self) -> int:
        with self._tasks_lock:
            return len(self._active_tasks)

    @property
    @topic_config()
    def message(self) -> str:
        return self.data.get("message", "")
