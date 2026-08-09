"""LaiYu PLR 后端 — 对齐路径 B 硬件交互模式

硬件初始化顺序与 laiyu_liquid_station.py (路径 B) 一致:
  1. XYZController(auto_connect=True) — 先开串口
  2. PipetteController.connect_shared() — 共享 XYZ 的串口 / 锁
  3. home_all_axes() + pipette.initialize()
"""

import logging
from typing import List, Optional, Union

from pylabrobot.liquid_handling.backends.backend import LiquidHandlerBackend
from pylabrobot.liquid_handling.standard import (
    Drop,
    DropTipRack,
    MultiHeadAspirationContainer,
    MultiHeadAspirationPlate,
    MultiHeadDispenseContainer,
    MultiHeadDispensePlate,
    Pickup,
    PickupTipRack,
    ResourceDrop,
    ResourceMove,
    ResourcePickup,
    SingleChannelAspiration,
    SingleChannelDispense,
)
from pylabrobot.resources import Resource, Tip

from unilabos.devices.liquid_handling.laiyu.controllers.xyz_controller import XYZController
from unilabos.devices.liquid_handling.laiyu.controllers.pipette_controller import (
    PipetteController,
    TipStatus,
)

logger = logging.getLogger(__name__)


class UniLiquidHandlerLaiyuBackend(LiquidHandlerBackend):
    """LaiYu 硬件后端 — PLR Backend 接口实现"""

    def __init__(
        self,
        num_channels: int = 1,
        tip_length: float = 0,
        total_height: float = 310,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        pipette_address: int = 4,
    ):
        super().__init__()
        self._num_channels = num_channels
        self.tip_length = tip_length
        self.total_height = total_height

        # 保存配置，延迟到 setup() 再创建硬件对象
        self._port = port
        self._baudrate = baudrate
        self._pipette_address = pipette_address

        self._xyz: Optional[XYZController] = None
        self._pipette_ctrl: Optional[PipetteController] = None
        self._ros_node = None

    # ------------------------------------------------------------------ lifecycle

    def post_init(self, ros_node):
        """接收 ROS 节点引用（由 Handler.post_init 调用）"""
        self._ros_node = ros_node

    async def setup(self):
        """按路径 B 顺序初始化硬件"""
        await super().setup()

        # 1. XYZ 先开串口
        self._xyz = XYZController(
            port=self._port,
            baudrate=self._baudrate,
            auto_connect=True,
        )
        if not self._xyz.is_connected:
            raise RuntimeError("XYZ 控制器连接失败")

        # 2. PipetteController 共享 XYZ 串口
        self._pipette_ctrl = PipetteController(
            port=self._port,
            address=self._pipette_address,
        )
        self._pipette_ctrl.connect_shared(
            serial_conn=self._xyz.serial_conn,
            serial_lock=self._xyz.serial_lock,
            xyz_controller=self._xyz,
        )

        # 3. 回零 + 移液器初始化
        self._xyz.home_all_axes()
        self._pipette_ctrl.initialize()

        logger.info("LaiYu 后端硬件初始化完成")

    async def stop(self):
        """正确断开硬件"""
        try:
            if self._pipette_ctrl:
                self._pipette_ctrl.disconnect_shared()
            if self._xyz:
                self._xyz.disconnect()
            logger.info("LaiYu 后端硬件已断开")
        except Exception as e:
            logger.error(f"停止后端失败: {e}")

    # ------------------------------------------------------------------ helpers

    def _plr_to_machine_coords(self, resource, offset):
        """PLR Resource 坐标 → 机器坐标 (倒置龙门架: total_height - z, -y)"""
        coordinate = resource.get_absolute_location(x="c", y="c")
        x = coordinate.x + offset.x
        y = coordinate.y + offset.y
        z_plr = coordinate.z + offset.z
        return x, -y, self.total_height - (z_plr + self.tip_length)

    def _pipette_aspirate(self, volume: float, flow_rate: float):
        self._pipette_ctrl.pipette.set_max_speed(flow_rate)
        res = self._pipette_ctrl.pipette.aspirate(volume=volume)
        if not res:
            logger.error(f"吸取失败，当前体积: {self._pipette_ctrl.current_volume}")
            return
        self._pipette_ctrl.current_volume += volume

    def _pipette_dispense(self, volume: float, flow_rate: float):
        self._pipette_ctrl.pipette.set_max_speed(flow_rate)
        res = self._pipette_ctrl.pipette.dispense(volume=volume)
        if not res:
            logger.error(f"排液失败，当前体积: {self._pipette_ctrl.current_volume}")
            return
        self._pipette_ctrl.current_volume -= volume

    # ------------------------------------------------------------------ properties

    def serialize(self) -> dict:
        return {**super().serialize(), "num_channels": self.num_channels}

    @property
    def num_channels(self) -> int:
        return self._num_channels

    # ------------------------------------------------------------------ resource callbacks

    async def assigned_resource_callback(self, resource: Resource):
        logger.info(f"Resource {resource.name} was assigned to the liquid handler.")

    async def unassigned_resource_callback(self, name: str):
        logger.info(f"Resource {name} was unassigned from the liquid handler.")

    # ------------------------------------------------------------------ pick_up_tips

    async def pick_up_tips(self, ops: List[Pickup], use_channels: List[int], **backend_kwargs):
        tip = ops[0].tip
        self.tip_length = tip.total_tip_length
        x, y, z_top = self._plr_to_machine_coords(ops[0].resource, ops[0].offset)

        self._pipette_ctrl._update_tip_status()
        if self._pipette_ctrl.tip_status == TipStatus.TIP_ATTACHED:
            logger.warning("已有枪头，无需重复拾取")
            return

        try:
            # 1. 移到枪头正上方
            self._xyz.move_to_work_coord_safe(x=x, y=y, z=z_top, speed=200)
            # 2. 下压到套枪头深度（fitting_depth 是枪头套入长度）
            z_pickup = z_top + tip.fitting_depth
            self._xyz.move_to_work_coord_safe(z=z_pickup, speed=100)
            # 3. 退回安全高度
            self._xyz.move_to_work_coord_safe(
                z=self._xyz.machine_config.safe_z_height, speed=100
            )
        except Exception as e:
            logger.error(f"pick_up_tips 移动失败: {e}")
            raise

    # ------------------------------------------------------------------ drop_tips

    async def drop_tips(self, ops: List[Drop], use_channels: List[int], **backend_kwargs):
        x, y, z = self._plr_to_machine_coords(ops[0].resource, ops[0].offset)
        z -= 20  # 额外下移补偿

        self._pipette_ctrl._update_tip_status()
        if self._pipette_ctrl.tip_status == TipStatus.NO_TIP:
            logger.warning("无枪头，无需丢弃")
            return

        try:
            self._xyz.move_to_work_coord_safe(x=x, y=y, z=z, speed=200)
            self._pipette_ctrl.eject_tip()  # 修复: 原来缺少 ()
            self._xyz.move_to_work_coord_safe(
                z=self._xyz.machine_config.safe_z_height
            )
        except Exception as e:
            logger.error(f"drop_tips 失败: {e}")
            raise

    # ------------------------------------------------------------------ aspirate

    async def aspirate(
        self,
        ops: List[SingleChannelAspiration],
        use_channels: List[int],
        **backend_kwargs,
    ):
        x, y, z = self._plr_to_machine_coords(ops[0].resource, ops[0].offset)

        self._pipette_ctrl._update_tip_status()
        if self._pipette_ctrl.tip_status != TipStatus.TIP_ATTACHED:
            raise RuntimeError("无枪头，无法吸液")

        flow_rate = backend_kwargs.get("flow_rate", 500)
        blow_out_air_volume = backend_kwargs.get("blow_out_air_volume", 0)

        if (
            self._pipette_ctrl.current_volume + ops[0].volume + blow_out_air_volume
            > self._pipette_ctrl.max_volume
        ):
            raise RuntimeError(
                f"吸液量超过枪头容量: "
                f"{self._pipette_ctrl.current_volume + ops[0].volume} > {self._pipette_ctrl.max_volume}"
            )

        self._xyz.move_to_work_coord_safe(x=x, y=y, z=z, speed=200)
        self._pipette_aspirate(volume=ops[0].volume, flow_rate=flow_rate)

        self._xyz.move_to_work_coord_safe(
            z=self._xyz.machine_config.safe_z_height
        )
        if blow_out_air_volume > 0:
            self._pipette_aspirate(volume=blow_out_air_volume, flow_rate=flow_rate)

    # ------------------------------------------------------------------ dispense

    async def dispense(
        self,
        ops: List[SingleChannelDispense],
        use_channels: List[int],
        **backend_kwargs,
    ):
        x, y, z = self._plr_to_machine_coords(ops[0].resource, ops[0].offset)

        self._pipette_ctrl._update_tip_status()
        if self._pipette_ctrl.tip_status != TipStatus.TIP_ATTACHED:
            raise RuntimeError("无枪头，无法排液")

        flow_rate = backend_kwargs.get("flow_rate", 500)
        blow_out_air_volume = backend_kwargs.get("blow_out_air_volume", 0)

        if (
            self._pipette_ctrl.current_volume - ops[0].volume - blow_out_air_volume < 0
        ):
            raise RuntimeError(
                f"排液量超过当前体积: "
                f"{self._pipette_ctrl.current_volume - ops[0].volume - blow_out_air_volume} < 0"
            )

        self._xyz.move_to_work_coord_safe(x=x, y=y, z=z, speed=200)
        self._pipette_dispense(volume=ops[0].volume, flow_rate=flow_rate)

        self._xyz.move_to_work_coord_safe(
            z=self._xyz.machine_config.safe_z_height
        )
        if blow_out_air_volume > 0:
            self._pipette_dispense(volume=blow_out_air_volume, flow_rate=flow_rate)

    # ------------------------------------------------------------------ 96-channel stubs

    async def pick_up_tips96(self, pickup: PickupTipRack, **backend_kwargs):
        logger.info(f"Picking up tips from {pickup.resource.name}.")

    async def drop_tips96(self, drop: DropTipRack, **backend_kwargs):
        logger.info(f"Dropping tips to {drop.resource.name}.")

    async def aspirate96(
        self, aspiration: Union[MultiHeadAspirationPlate, MultiHeadAspirationContainer]
    ):
        if isinstance(aspiration, MultiHeadAspirationPlate):
            resource = aspiration.wells[0].parent
        else:
            resource = aspiration.container
        logger.info(f"Aspirating {aspiration.volume} from {resource}.")

    async def dispense96(
        self, dispense: Union[MultiHeadDispensePlate, MultiHeadDispenseContainer]
    ):
        if isinstance(dispense, MultiHeadDispensePlate):
            resource = dispense.wells[0].parent
        else:
            resource = dispense.container
        logger.info(f"Dispensing {dispense.volume} to {resource}.")

    async def pick_up_resource(self, pickup: ResourcePickup):
        logger.info(f"Picking up resource: {pickup}")

    async def move_picked_up_resource(self, move: ResourceMove):
        logger.info(f"Moving picked up resource: {move}")

    async def drop_resource(self, drop: ResourceDrop):
        logger.info(f"Dropping resource: {drop}")

    def can_pick_up_tip(self, channel_idx: int, tip: Tip) -> bool:
        return True
