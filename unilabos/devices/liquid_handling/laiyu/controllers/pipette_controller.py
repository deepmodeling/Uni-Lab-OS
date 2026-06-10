#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
移液控制器模块
封装SOPA移液器的高级控制功能
"""

# 添加项目根目录到Python路径以解决模块导入问题
from unilabos.devices.liquid_handling.laiyu.controllers.xyz_controller import (
    XYZController,
    MotorAxis,
    MotorStatus
)
from unilabos.devices.liquid_handling.laiyu.drivers.sopa_pipette_driver import (
    SOPAPipette,
    SOPAConfig,
    SOPAStatusCode,
    DetectionMode,
    create_sopa_pipette,
)
from enum import Enum
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
import logging
import time
import sys
import os
from tkinter import N

from unilabos.devices.liquid_handling.laiyu.drivers.xyz_stepper_driver import ModbusException

# 无论如何都添加项目根目录到路径
current_file = os.path.abspath(__file__)
# 从 .../Uni-Lab-OS/unilabos/devices/LaiYu_Liquid/controllers/pipette_controller.py
# 向上5级到 .../Uni-Lab-OS
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_file)))))
# 强制添加项目根目录到sys.path的开头
sys.path.insert(0, project_root)


# from unilabos.devices.liquid_handling.laiyu.drivers.xyz_stepper_driver import (
#     XYZStepperController,
#     MotorAxis,
#     MotorStatus,
#     ModbusException
# )


logger = logging.getLogger(__name__)


class TipStatus(Enum):
    """枪头状态"""
    NO_TIP = "no_tip"
    TIP_ATTACHED = "tip_attached"
    TIP_USED = "tip_used"


class LiquidClass(Enum):
    """液体类型"""
    WATER = "water"
    SERUM = "serum"
    VISCOUS = "viscous"
    VOLATILE = "volatile"
    CUSTOM = "custom"


@dataclass
class LiquidParameters:
    """液体处理参数"""
    aspirate_speed: int = 500      # 吸液速度
    dispense_speed: int = 800      # 排液速度
    air_gap: float = 10.0          # 空气间隙
    blow_out: float = 5.0          # 吹出量
    pre_wet: bool = False          # 预润湿
    mix_cycles: int = 0            # 混合次数
    mix_volume: float = 50.0       # 混合体积
    touch_tip: bool = False        # 接触壁
    delay_after_aspirate: float = 0.5  # 吸液后延时
    delay_after_dispense: float = 0.5  # 排液后延时


class PipetteController:
    """移液控制器"""

    # 预定义液体参数
    LIQUID_PARAMS = {
        LiquidClass.WATER: LiquidParameters(
            aspirate_speed=500,
            dispense_speed=800,
            air_gap=10.0
        ),
        LiquidClass.SERUM: LiquidParameters(
            aspirate_speed=200,
            dispense_speed=400,
            air_gap=15.0,
            pre_wet=True,
            delay_after_aspirate=1.0
        ),
        LiquidClass.VISCOUS: LiquidParameters(
            aspirate_speed=100,
            dispense_speed=200,
            air_gap=20.0,
            delay_after_aspirate=2.0,
            delay_after_dispense=2.0
        ),
        LiquidClass.VOLATILE: LiquidParameters(
            aspirate_speed=800,
            dispense_speed=1000,
            air_gap=5.0,
            delay_after_aspirate=0.2,
            delay_after_dispense=0.2
        )
    }

    def __init__(self, port: str, address: int = 4, xyz_port: Optional[str] = None):
        """
        初始化移液控制器

        Args:
            port: 移液器串口端口
            address: 移液器RS485地址
            xyz_port: XYZ步进电机串口端口（可选，用于枪头装载等运动控制）
        """
        self.config = SOPAConfig(
            port=port,
            address=address,
            baudrate=115200
        )
        self.pipette = SOPAPipette(self.config)
        self.pipette_port = port
        self.tip_status = TipStatus.NO_TIP
        self.current_volume = 0.0
        self.max_volume = 1000.0  # 默认1000ul
        self.liquid_class = LiquidClass.WATER
        self.liquid_params = self.LIQUID_PARAMS[LiquidClass.WATER]

        # XYZ步进电机控制器（用于运动控制）
        self.xyz_controller: Optional[XYZController] = None
        self.xyz_port = xyz_port if xyz_port else port
        self.xyz_connected = True

        # 统计信息
        # self.tip_count = 0
        self.aspirate_count = 0
        self.dispense_count = 0

    def connect(self) -> bool:
        """连接移液器和XYZ步进电机控制器"""
        try:
            # 连接移液器
            if not self.pipette.connect():
                logger.error("移液器连接失败")
                return False
            logger.info("移液器连接成功")

            # 连接XYZ步进电机控制器（如果提供了端口）
            if self.xyz_port != self.pipette_port:
                try:
                    self.xyz_controller = XYZController(self.xyz_port)
                    if self.xyz_controller.connect():
                        self.xyz_connected = True
                        logger.info(f"XYZ步进电机控制器连接成功: {self.xyz_port}")
                    else:
                        logger.warning(f"XYZ步进电机控制器连接失败: {self.xyz_port}")
                        self.xyz_controller = None
                except Exception as e:
                    logger.warning(f"XYZ步进电机控制器连接异常: {e}")
                    self.xyz_controller = None
                    self.xyz_connected = False
            else:
                try:
                    self.xyz_controller = XYZController(self.xyz_port, auto_connect=False)
                    self.xyz_controller.serial_conn = self.pipette.serial_port
                    self.xyz_controller.is_connected = True
                except Exception as e:
                    logger.info("未配置XYZ步进电机端口，跳过运动控制器连接")

            return True
        except Exception as e:
            logger.error(f"设备连接失败: {e}")
            return False

    def initialize(self) -> bool:
        """初始化移液器"""
        try:
            if self.pipette.initialize():
                logger.info("移液器初始化成功")
                # 检查枪头状态
                self._update_tip_status()
                self.xyz_controller.home_all_axes()
                self.xyz_controller.move_to_work_coord_safe(x=0, y=-150, z=0)
                return True
            return False
        except Exception as e:
            logger.error(f"移液器初始化失败: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        # 断开移液器连接
        self.pipette.disconnect()
        logger.info("移液器已断开")

        # 断开 XYZ 步进电机连接
        if self.xyz_controller and self.xyz_connected:
            try:
                self.xyz_controller.disconnect()
                self.xyz_connected = False
                logger.info("XYZ 步进电机已断开")
            except Exception as e:
                logger.error(f"断开 XYZ 步进电机失败: {e}")

    def _check_xyz_safety(self, axis: MotorAxis, target_position: int) -> bool:
        """
        检查 XYZ 轴移动的安全性

        Args:
            axis: 电机轴
            target_position: 目标位置(步数)

        Returns:
            是否安全
        """
        try:
            # 获取当前电机状态
            motor_position = self.xyz_controller.get_motor_status(axis)

            # 检查电机状态是否正常 (不是碰撞停止或限位停止)
            if motor_position.status in [MotorStatus.COLLISION_STOP,
                                         MotorStatus.FORWARD_LIMIT_STOP,
                                         MotorStatus.REVERSE_LIMIT_STOP]:
                logger.error(f"{axis.name} 轴电机处于错误状态: {motor_position.status.name}")
                return False

            # 检查位置限制 (扩大安全范围以适应实际硬件)
            # 步进电机的位置范围通常很大，这里设置更合理的范围
            if target_position < -500000 or target_position > 500000:
                logger.error(f"{axis.name} 轴目标位置超出安全范围: {target_position}")
                return False

            # 检查移动距离是否过大 (单次移动不超过 20000 步，约12mm)
            current_position = motor_position.steps
            move_distance = abs(target_position - current_position)
            if move_distance > 20000:
                logger.error(f"{axis.name} 轴单次移动距离过大: {move_distance}步")
                return False

            return True

        except Exception as e:
            logger.error(f"安全检查失败: {e}")
            return False

    def move_z_relative(self, distance_mm: float, speed: int = 2000, acceleration: int = 500) -> bool:
        """
        Z轴相对移动

        Args:
            distance_mm: 移动距离(mm)，正值向下，负值向上
            speed: 移动速度(rpm)
            acceleration: 加速度(rpm/s)

        Returns:
            移动是否成功
        """
        if not self.xyz_controller or not self.xyz_connected:
            logger.error("XYZ 步进电机未连接，无法执行移动")
            return False

        try:
            # 参数验证
            if abs(distance_mm) > 15.0:
                logger.error(f"移动距离过大: {distance_mm}mm，最大允许15mm")
                return False

            if speed < 100 or speed > 5000:
                logger.error(f"速度参数无效: {speed}rpm，范围应为100-5000")
                return False

            # 获取当前 Z 轴位置
            current_status = self.xyz_controller.get_motor_status(MotorAxis.Z)
            current_z_position = current_status.steps

            # 计算移动距离对应的步数 (1mm = 1638.4步)
            mm_to_steps = 1638.4
            move_distance_steps = int(distance_mm * mm_to_steps)

            # 计算目标位置
            target_z_position = current_z_position + move_distance_steps

            # 安全检查
            if not self._check_xyz_safety(MotorAxis.Z, target_z_position):
                logger.error("Z轴移动安全检查失败")
                return False

            logger.info(f"Z轴相对移动: {distance_mm}mm ({move_distance_steps}步)")
            logger.info(f"当前位置: {current_z_position}步 -> 目标位置: {target_z_position}步")

            # 执行移动
            success = self.xyz_controller.move_to_position(
                axis=MotorAxis.Z,
                position=target_z_position,
                speed=speed,
                acceleration=acceleration,
                precision=50
            )

            if not success:
                logger.error("Z轴移动命令发送失败")
                return False

            # 等待移动完成
            if not self.xyz_controller.wait_for_completion(MotorAxis.Z, timeout=10.0):
                logger.error("Z轴移动超时")
                return False

            # 验证移动结果
            final_status = self.xyz_controller.get_motor_status(MotorAxis.Z)
            final_position = final_status.steps
            position_error = abs(final_position - target_z_position)

            logger.info(f"Z轴移动完成，最终位置: {final_position}步，误差: {position_error}步")

            if position_error > 100:
                logger.warning(f"Z轴位置误差较大: {position_error}步")

            return True

        except ModbusException as e:
            logger.error(f"Modbus通信错误: {e}")
            return False
        except Exception as e:
            logger.error(f"Z轴移动失败: {e}")
            return False

    def emergency_stop(self) -> bool:
        """
        紧急停止所有运动

        Returns:
            停止是否成功
        """
        success = True

        # 停止移液器操作
        try:
            if self.pipette and self.connected:
                # 这里可以添加移液器的紧急停止逻辑
                logger.info("移液器紧急停止")
        except Exception as e:
            logger.error(f"移液器紧急停止失败: {e}")
            success = False

        # 停止 XYZ 轴运动
        try:
            if self.xyz_controller and self.xyz_connected:
                self.xyz_controller.emergency_stop()
                logger.info("XYZ 轴紧急停止")
        except Exception as e:
            logger.error(f"XYZ 轴紧急停止失败: {e}")
            success = False

        return success

    def pickup_tip(self) -> bool:
        """
        装载枪头 - Z轴向下移动10mm进行枪头装载

        Returns:
            是否成功
        """
        self._update_tip_status()
        if self.tip_status == TipStatus.TIP_ATTACHED:
            logger.warning("已有枪头，无需重复装载")
            return True

        logger.info("开始装载枪头 - Z轴向下移动10mm")

        # 使用相对移动方法，向下移动10mm
        if self.move_z_relative(distance_mm=10.0, speed=2000, acceleration=500):
            # 更新枪头状态
            self._update_tip_status()
            # self.tip_status = TipStatus.TIP_ATTACHED
            # self.tip_count += 1
            self.current_volume = 0.0
            if self.tip_status == TipStatus.TIP_ATTACHED:
                logger.info("枪头装载成功")
                return True
            else:
                logger.info("枪头装载失败")
                return False
        else:
            logger.error("枪头装载失败 - Z轴移动失败")
            return False

    def eject_tip(self) -> bool:
        """
        弹出枪头

        Returns:
            是否成功
        """

        self._update_tip_status()

        if self.tip_status == TipStatus.NO_TIP:
            logger.warning("无枪头可弹出")
            return True

        try:
            if self.pipette.eject_tip():
                self._update_tip_status()
                if self.tip_status == TipStatus.NO_TIP:
                    self.current_volume = 0.0
                    logger.info("枪头已弹出")
                    return True
            return False
        except Exception as e:
            logger.error(f"弹出枪头失败: {e}")
            return False

    def aspirate(self, volume: float, liquid_class: Optional[LiquidClass] = None,
                 detection: bool = True) -> bool:
        """
        吸液

        Args:
            volume: 吸液体积(ul)
            liquid_class: 液体类型
            detection: 是否开启液位检测

        Returns:
            是否成功
        """
        self._update_tip_status()
        if self.tip_status != TipStatus.TIP_ATTACHED:
            logger.error("无枪头，无法吸液")
            return False

        if self.current_volume + volume > self.max_volume:
            logger.error(f"吸液量超过枪头容量: {self.current_volume + volume} > {self.max_volume}")
            return False

        # 设置液体参数
        if liquid_class:
            self.set_liquid_class(liquid_class)

        try:
            # 设置吸液速度
            self.pipette.set_max_speed(self.liquid_params.aspirate_speed)

            # 执行液位检测
            if detection:
                if not self.pipette.liquid_level_detection():
                    logger.warning("液位检测失败，继续吸液")

            # 预润湿
            if self.liquid_params.pre_wet and self.current_volume == 0:
                logger.info("执行预润湿")
                self._pre_wet(volume * 0.2)

            # 吸液
            if self.pipette.aspirate(volume, detection=False):
                self.current_volume += volume
                self.aspirate_count += 1

                # 吸液后延时
                time.sleep(self.liquid_params.delay_after_aspirate)

                # 吸取空气间隙
                if self.liquid_params.air_gap > 0:
                    self.pipette.aspirate(self.liquid_params.air_gap, detection=False)
                    self.current_volume += self.liquid_params.air_gap

                logger.info(f"吸液完成: {volume}ul, 当前体积: {self.current_volume}ul")
                return True
            else:
                logger.error("吸液失败")
                return False

        except Exception as e:
            logger.error(f"吸液异常: {e}")
            return False

    def dispense(self, volume: float, blow_out: bool = False) -> bool:
        """
        排液

        Args:
            volume: 排液体积(ul)
            blow_out: 是否吹出

        Returns:
            是否成功
        """
        self._update_tip_status()
        if self.tip_status != TipStatus.TIP_ATTACHED:
            logger.error("无枪头，无法排液")
            return False

        if volume > self.current_volume:
            logger.error(f"排液量超过当前体积: {volume} > {self.current_volume}")
            return False

        try:
            # 设置排液速度
            self.pipette.set_max_speed(self.liquid_params.dispense_speed)

            # 排液
            if self.pipette.dispense(volume):
                self.current_volume -= volume
                self.dispense_count += 1

                # 排液后延时
                time.sleep(self.liquid_params.delay_after_dispense)

                # 吹出
                if blow_out and self.liquid_params.blow_out > 0:
                    self.pipette.dispense(self.liquid_params.blow_out)
                    logger.debug(f"执行吹出: {self.liquid_params.blow_out}ul")

                # 接触壁
                if self.liquid_params.touch_tip:
                    self._touch_tip()

                logger.info(f"排液完成: {volume}ul, 剩余体积: {self.current_volume}ul")
                return True
            else:
                logger.error("排液失败")
                return False

        except Exception as e:
            logger.error(f"排液异常: {e}")
            return False

    def transfer(self, volume: float,
                 source_well: Optional[str] = None,
                 dest_well: Optional[str] = None,
                 liquid_class: Optional[LiquidClass] = None,
                 new_tip: bool = True,
                 mix_before: Optional[Tuple[int, float]] = None,
                 mix_after: Optional[Tuple[int, float]] = None) -> bool:
        """
        液体转移

        Args:
            volume: 转移体积
            source_well: 源孔位
            dest_well: 目标孔位
            liquid_class: 液体类型
            new_tip: 是否使用新枪头
            mix_before: 吸液前混合(次数, 体积)
            mix_after: 排液后混合(次数, 体积)

        Returns:
            是否成功
        """
        try:
            # 装载新枪头
            if new_tip:
                self.eject_tip()
                if not self.pickup_tip():
                    return False

            # 设置液体类型
            if liquid_class:
                self.set_liquid_class(liquid_class)

            # 吸液前混合
            if mix_before:
                cycles, mix_vol = mix_before
                self.mix(cycles, mix_vol)

            # 吸液
            if not self.aspirate(volume):
                return False

            # 排液
            if not self.dispense(volume, blow_out=True):
                return False

            # 排液后混合
            if mix_after:
                cycles, mix_vol = mix_after
                self.mix(cycles, mix_vol)

            logger.info(f"液体转移完成: {volume}ul")
            return True

        except Exception as e:
            logger.error(f"液体转移失败: {e}")
            return False

    def mix(self, cycles: int = 3, volume: Optional[float] = None) -> bool:
        """
        混合

        Args:
            cycles: 混合次数
            volume: 混合体积

        Returns:
            是否成功
        """
        volume = volume or self.liquid_params.mix_volume

        logger.info(f"开始混合: {cycles}次, {volume}ul")

        for i in range(cycles):
            if not self.aspirate(volume, detection=False):
                return False
            if not self.dispense(volume):
                return False

        logger.info("混合完成")
        return True

    def _pre_wet(self, volume: float):
        """预润湿"""
        self.pipette.aspirate(volume, detection=False)
        time.sleep(0.2)
        self.pipette.dispense(volume)
        time.sleep(0.2)

    def _touch_tip(self):
        """接触壁(需要与运动控制配合)"""
        # TODO: 实现接触壁动作
        logger.debug("执行接触壁")
        time.sleep(0.5)

    def _update_tip_status(self):
        """更新枪头状态"""
        if self.pipette.get_tip_status():
            self.tip_status = TipStatus.TIP_ATTACHED
        else:
            self.tip_status = TipStatus.NO_TIP

    def set_liquid_class(self, liquid_class: LiquidClass):
        """设置液体类型"""
        self.liquid_class = liquid_class
        if liquid_class in self.LIQUID_PARAMS:
            self.liquid_params = self.LIQUID_PARAMS[liquid_class]
        logger.info(f"液体类型设置为: {liquid_class.value}")

    def set_custom_parameters(self, params: LiquidParameters):
        """设置自定义液体参数"""
        self.liquid_params = params
        self.liquid_class = LiquidClass.CUSTOM

    def calibrate_volume(self, expected: float, actual: float):
        """
        体积校准

        Args:
            expected: 期望体积
            actual: 实际体积
        """
        factor = actual / expected
        self.pipette.set_calibration_factor(factor)
        logger.info(f"体积校准系数: {factor}")

    def get_status(self) -> Dict:
        """获取状态信息"""
        self._update_tip_status()
        return {
            'tip_status': self.tip_status.value,
            'current_volume': self.current_volume,
            'max_volume': self.max_volume,
            'liquid_class': self.liquid_class.value,
            'statistics': {
                # 'tip_count': self.tip_count,
                'aspirate_count': self.aspirate_count,
                'dispense_count': self.dispense_count
            }
        }

    def reset_statistics(self):
        """重置统计信息"""
        # self.tip_count = 0
        self.aspirate_count = 0
        self.dispense_count = 0

# ============================================================================
# 实例化代码块 - 移液控制器使用示例
# ============================================================================


if __name__ == "__main__":
    # 配置日志
    import logging

    # 设置日志级别
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    def interactive_test():
        """交互式测试模式 - 适用于已连接的设备"""
        print("\n" + "=" * 60)
        print("🧪 移液器交互式测试模式")
        print("=" * 60)

        # 获取用户输入的连接参数
        print("\n📡 设备连接配置:")
        port = input("请输入移液器串口端口 (默认: /dev/ttyUSB_CH340): ").strip() or "/dev/ttyUSB_CH340"
        address_input = input("请输入移液器设备地址 (默认: 4): ").strip()
        address = int(address_input) if address_input else 4

        # 询问是否连接 XYZ 步进电机控制器
        xyz_enable = input("是否连接 XYZ 步进电机控制器? (y/N): ").strip().lower()
        xyz_port = None
        if xyz_enable not in ['n', 'no']:
            xyz_port = input("请输入 XYZ 控制器串口端口 (默认: /dev/ttyUSB_CH340): ").strip() or "/dev/ttyUSB_CH340"

        try:
            # 创建移液控制器实例
            if xyz_port:
                print(f"\n🔧 创建移液控制器实例 (移液器端口: {port}, 地址: {address}, XYZ端口: {xyz_port})...")
                pipette = PipetteController(port=port, address=address, xyz_port=xyz_port)
            else:
                print(f"\n🔧 创建移液控制器实例 (端口: {port}, 地址: {address})...")
                pipette = PipetteController(port=port, address=address)

            # 连接设备
            print("\n📞 连接移液器设备...")
            if not pipette.connect():
                print("❌ 设备连接失败，请检查连接")
                return
            print("✅ 设备连接成功")

            # 初始化设备
            print("\n🚀 初始化设备...")
            if not pipette.initialize():
                print("❌ 设备初始化失败")
                return
            print("✅ 设备初始化成功")

            # 交互式菜单
            while True:
                print("\n" + "=" * 50)
                print("🎮 交互式操作菜单:")
                print("1. 📋 查看设备状态")
                print("2. 🔧 装载枪头")
                print("3. 🗑️  弹出枪头")
                print("4. 💧 吸液操作")
                print("5. 💦 排液操作")
                print("6. 🌀 混合操作")
                print("7. 🔄 液体转移")
                print("8. ⚙️  设置液体类型")
                print("9. 🎯 自定义参数")
                print("10. 📊 校准体积")
                print("11. 🧹 重置统计")
                print("12. 🔍 液体类型测试")
                print("99. 🚨 紧急停止")
                print("0. 🚪 退出程序")
                print("=" * 50)

                choice = input("\n请选择操作 (0-12, 99): ").strip()

                if choice == "0":
                    print("\n👋 退出程序...")
                    break
                elif choice == "1":
                    # 查看设备状态
                    status = pipette.get_status()
                    print("\n📊 设备状态信息:")
                    print(f"  🎯 枪头状态: {status['tip_status']}")
                    print(f"  💧 当前体积: {status['current_volume']}ul")
                    print(f"  📏 最大体积: {status['max_volume']}ul")
                    print(f"  🧪 液体类型: {status['liquid_class']}")
                    print(f"  📈 统计信息:")
                    # print(f"    🔧 枪头使用次数: {status['statistics']['tip_count']}")
                    print(f"    ⬆️  吸液次数: {status['statistics']['aspirate_count']}")
                    print(f"    ⬇️  排液次数: {status['statistics']['dispense_count']}")

                elif choice == "2":
                    # 装载枪头
                    print("\n🔧 装载枪头...")
                    if pipette.xyz_connected:
                        print("📍 使用 XYZ 控制器进行 Z 轴定位 (下移 10mm)")
                    else:
                        print("⚠️  未连接 XYZ 控制器，仅执行移液器枪头装载")

                    if pipette.pickup_tip():
                        print("✅ 枪头装载成功")
                        if pipette.xyz_connected:
                            print("📍 Z 轴已移动到装载位置")
                    else:
                        print("❌ 枪头装载失败")

                elif choice == "3":
                    # 弹出枪头
                    print("\n🗑️ 弹出枪头...")
                    if pipette.eject_tip():
                        print("✅ 枪头弹出成功")
                    else:
                        print("❌ 枪头弹出失败")

                elif choice == "4":
                    # 吸液操作
                    try:
                        volume = float(input("请输入吸液体积 (ul): "))
                        detection = input("是否启用液面检测? (y/n, 默认y): ").strip().lower() != 'n'
                        print(f"\n💧 执行吸液操作 ({volume}ul)...")
                        if pipette.aspirate(volume, detection=detection):
                            print(f"✅ 吸液成功: {volume}ul")
                            print(f"📊 当前体积: {pipette.current_volume}ul")
                        else:
                            print("❌ 吸液失败")
                    except ValueError:
                        print("❌ 请输入有效的数字")

                elif choice == "5":
                    # 排液操作
                    try:
                        volume = float(input("请输入排液体积 (ul): "))
                        blow_out = input("是否执行吹出操作? (y/n, 默认n): ").strip().lower() == 'y'
                        print(f"\n💦 执行排液操作 ({volume}ul)...")
                        if pipette.dispense(volume, blow_out=blow_out):
                            print(f"✅ 排液成功: {volume}ul")
                            print(f"📊 剩余体积: {pipette.current_volume}ul")
                        else:
                            print("❌ 排液失败")
                    except ValueError:
                        print("❌ 请输入有效的数字")

                elif choice == "6":
                    # 混合操作
                    try:
                        cycles = int(input("请输入混合次数 (默认3): ") or "3")
                        volume_input = input("请输入混合体积 (ul, 默认使用当前体积的50%): ").strip()
                        volume = float(volume_input) if volume_input else None
                        print(f"\n🌀 执行混合操作 ({cycles}次)...")
                        if pipette.mix(cycles=cycles, volume=volume):
                            print("✅ 混合完成")
                        else:
                            print("❌ 混合失败")
                    except ValueError:
                        print("❌ 请输入有效的数字")

                elif choice == "7":
                    # 液体转移
                    try:
                        volume = float(input("请输入转移体积 (ul): "))
                        source = input("源孔位 (可选, 如A1): ").strip() or None
                        dest = input("目标孔位 (可选, 如B1): ").strip() or None
                        new_tip = input("是否使用新枪头? (y/n, 默认y): ").strip().lower() != 'n'

                        print(f"\n🔄 执行液体转移 ({volume}ul)...")
                        if pipette.transfer(volume=volume, source_well=source, dest_well=dest, new_tip=new_tip):
                            print("✅ 液体转移完成")
                        else:
                            print("❌ 液体转移失败")
                    except ValueError:
                        print("❌ 请输入有效的数字")

                elif choice == "8":
                    # 设置液体类型
                    print("\n🧪 可用液体类型:")
                    liquid_options = {
                        "1": (LiquidClass.WATER, "水溶液"),
                        "2": (LiquidClass.SERUM, "血清"),
                        "3": (LiquidClass.VISCOUS, "粘稠液体"),
                        "4": (LiquidClass.VOLATILE, "挥发性液体")
                    }

                    for key, (liquid_class, description) in liquid_options.items():
                        print(f"  {key}. {description}")

                    liquid_choice = input("请选择液体类型 (1-4): ").strip()
                    if liquid_choice in liquid_options:
                        liquid_class, description = liquid_options[liquid_choice]
                        pipette.set_liquid_class(liquid_class)
                        print(f"✅ 液体类型设置为: {description}")

                        # 显示参数
                        params = pipette.liquid_params
                        print(f"📋 参数设置:")
                        print(f"  ⬆️ 吸液速度: {params.aspirate_speed}")
                        print(f"  ⬇️ 排液速度: {params.dispense_speed}")
                        print(f"  💨 空气间隙: {params.air_gap}ul")
                        print(f"  💧 预润湿: {'是' if params.pre_wet else '否'}")
                    else:
                        print("❌ 无效选择")

                elif choice == "9":
                    # 自定义参数
                    try:
                        print("\n⚙️ 设置自定义参数 (直接回车使用默认值):")
                        aspirate_speed = input("吸液速度 (默认500): ").strip()
                        dispense_speed = input("排液速度 (默认800): ").strip()
                        air_gap = input("空气间隙 (ul, 默认10.0): ").strip()
                        pre_wet = input("预润湿 (y/n, 默认n): ").strip().lower() == 'y'

                        custom_params = LiquidParameters(
                            aspirate_speed=int(aspirate_speed) if aspirate_speed else 500,
                            dispense_speed=int(dispense_speed) if dispense_speed else 800,
                            air_gap=float(air_gap) if air_gap else 10.0,
                            pre_wet=pre_wet
                        )

                        pipette.set_custom_parameters(custom_params)
                        print("✅ 自定义参数设置完成")
                    except ValueError:
                        print("❌ 请输入有效的数字")

                elif choice == "10":
                    # 校准体积
                    try:
                        expected = float(input("期望体积 (ul): "))
                        actual = float(input("实际测量体积 (ul): "))
                        pipette.calibrate_volume(expected, actual)
                        print(f"✅ 校准完成，校准系数: {actual/expected:.3f}")
                    except ValueError:
                        print("❌ 请输入有效的数字")

                elif choice == "11":
                    # 重置统计
                    pipette.reset_statistics()
                    print("✅ 统计信息已重置")

                elif choice == "12":
                    # 液体类型测试
                    print("\n🧪 液体类型参数对比:")
                    liquid_tests = [
                        (LiquidClass.WATER, "水溶液"),
                        (LiquidClass.SERUM, "血清"),
                        (LiquidClass.VISCOUS, "粘稠液体"),
                        (LiquidClass.VOLATILE, "挥发性液体")
                    ]

                    for liquid_class, description in liquid_tests:
                        params = pipette.LIQUID_PARAMS[liquid_class]
                        print(f"\n📋 {description} ({liquid_class.value}):")
                        print(f"  ⬆️ 吸液速度: {params.aspirate_speed}")
                        print(f"  ⬇️ 排液速度: {params.dispense_speed}")
                        print(f"  💨 空气间隙: {params.air_gap}ul")
                        print(f"  💧 预润湿: {'是' if params.pre_wet else '否'}")
                        print(f"  ⏱️ 吸液后延时: {params.delay_after_aspirate}s")

                elif choice == "99":
                    # 紧急停止
                    print("\n🚨 执行紧急停止...")
                    success = pipette.emergency_stop()
                    if success:
                        print("✅ 紧急停止执行成功")
                        print("⚠️ 所有运动已停止，请检查设备状态")
                    else:
                        print("❌ 紧急停止执行失败")
                        print("⚠️ 请手动检查设备状态并采取必要措施")

                    # 紧急停止后询问是否继续
                    continue_choice = input("\n是否继续操作？(y/n): ").strip().lower()
                    if continue_choice != 'y':
                        print("🚪 退出程序")
                        break

                else:
                    print("❌ 无效选择，请重新输入")

                # 等待用户确认继续
                input("\n按回车键继续...")

        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断操作")
        except Exception as e:
            print(f"\n❌ 发生异常: {e}")
        finally:
            # 断开连接
            print("\n📞 断开设备连接...")
            try:
                pipette.disconnect()
                print("✅ 连接已断开")
            except:
                print("⚠️ 断开连接时出现问题")

    def demo_test():
        """演示测试模式 - 完整功能演示"""
        print("\n" + "=" * 60)
        print("🎬 移液控制器演示测试")
        print("=" * 60)

        try:
            # 创建移液控制器实例
            print("1. 🔧 创建移液控制器实例...")
            pipette = PipetteController(port="/dev/ttyUSB0", address=4)
            print("✅ 移液控制器实例创建成功")

            # 连接设备
            print("\n2. 📞 连接移液器设备...")
            if pipette.connect():
                print("✅ 设备连接成功")
            else:
                print("❌ 设备连接失败")
                return False

            # 初始化设备
            print("\n3. 🚀 初始化设备...")
            if pipette.initialize():
                print("✅ 设备初始化成功")
            else:
                print("❌ 设备初始化失败")
                return False

            # 装载枪头
            print("\n4. 🔧 装载枪头...")
            if pipette.pickup_tip():
                print("✅ 枪头装载成功")
            else:
                print("❌ 枪头装载失败")

            # 设置液体类型
            print("\n5. 🧪 设置液体类型为血清...")
            pipette.set_liquid_class(LiquidClass.SERUM)
            print("✅ 液体类型设置完成")

            # 吸液操作
            print("\n6. 💧 执行吸液操作...")
            volume_to_aspirate = 100.0
            if pipette.aspirate(volume_to_aspirate, detection=True):
                print(f"✅ 吸液成功: {volume_to_aspirate}ul")
                print(f"📊 当前体积: {pipette.current_volume}ul")
            else:
                print("❌ 吸液失败")

            # 排液操作
            print("\n7. 💦 执行排液操作...")
            volume_to_dispense = 50.0
            if pipette.dispense(volume_to_dispense, blow_out=True):
                print(f"✅ 排液成功: {volume_to_dispense}ul")
                print(f"📊 剩余体积: {pipette.current_volume}ul")
            else:
                print("❌ 排液失败")

            # 混合操作
            print("\n8. 🌀 执行混合操作...")
            if pipette.mix(cycles=3, volume=30.0):
                print("✅ 混合完成")
            else:
                print("❌ 混合失败")

            # 获取状态信息
            print("\n9. 📊 获取设备状态...")
            status = pipette.get_status()
            print("设备状态信息:")
            print(f"  🎯 枪头状态: {status['tip_status']}")
            print(f"  💧 当前体积: {status['current_volume']}ul")
            print(f"  📏 最大体积: {status['max_volume']}ul")
            print(f"  🧪 液体类型: {status['liquid_class']}")
            print(f"  📈 统计信息:")
            # print(f"    🔧 枪头使用次数: {status['statistics']['tip_count']}")
            print(f"    ⬆️ 吸液次数: {status['statistics']['aspirate_count']}")
            print(f"    ⬇️ 排液次数: {status['statistics']['dispense_count']}")

            # 弹出枪头
            print("\n10. 🗑️ 弹出枪头...")
            if pipette.eject_tip():
                print("✅ 枪头弹出成功")
            else:
                print("❌ 枪头弹出失败")

            print("\n" + "=" * 60)
            print("✅ 移液控制器演示测试完成")
            print("=" * 60)

            return True

        except Exception as e:
            print(f"\n❌ 测试过程中发生异常: {e}")
            return False

        finally:
            # 断开连接
            print("\n📞 断开连接...")
            pipette.disconnect()
            print("✅ 连接已断开")

    # 主程序入口
    print("🧪 移液器控制器测试程序")
    print("=" * 40)
    print("1. 🎮 交互式测试 (推荐)")
    print("2. 🎬 演示测试")
    print("0. 🚪 退出")
    print("=" * 40)

    mode = input("请选择测试模式 (0-2): ").strip()

    if mode == "1":
        interactive_test()
    elif mode == "2":
        demo_test()
    elif mode == "0":
        print("👋 再见！")
    else:
        print("❌ 无效选择")

    print("\n🎉 程序结束！")
    print("\n💡 使用说明:")
    print("1. 确保移液器硬件已正确连接")
    print("2. 根据实际情况修改串口端口号")
    print("3. 交互模式支持实时操作和参数调整")
    print("4. 在实际使用中需要配合运动控制器进行位置移动")
