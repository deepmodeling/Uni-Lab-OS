
import os
import time
import json
import logging
from xyz_stepper_driver import ModbusRTUTransport, ModbusClient, XYZStepperController, MotorStatus

# ========== 日志配置 ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("XYZ_Debug")


def create_controller(port: str = "/dev/ttyUSB1", baudrate: int = 115200) -> XYZStepperController:
    """
    初始化通信层与三轴控制器
    """
    logger.info(f"🔧 初始化控制器: {port} @ {baudrate}bps")
    transport = ModbusRTUTransport(port=port, baudrate=baudrate)
    transport.open()
    client = ModbusClient(transport)
    return XYZStepperController(client=client, port=port, baudrate=baudrate)


def load_existing_soft_zero(ctrl: XYZStepperController, path: str = "work_origin.json") -> bool:
    """
    如果已存在软零点文件则加载，否则返回 False
    """
    if not os.path.exists(path):
        logger.warning("⚠ 未找到已有软零点文件，将等待人工定义新零点。")
        return False

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        origin = data.get("work_origin_steps", {})
        ctrl.work_origin_steps = origin
        ctrl.is_homed = True
        logger.info(f"✔ 已加载软零点文件：{path}")
        logger.info(f"当前软零点步数: {origin}")
        return True
    except Exception as e:
        logger.error(f"读取软零点文件失败: {e}")
        return False


def test_enable_axis(ctrl: XYZStepperController):
    """
    依次使能 X / Y / Z 三轴
    """
    logger.info("=== 测试各轴使能 ===")
    for axis in ["X", "Y", "Z"]:
        try:
            result = ctrl.enable(axis, True)
            if result:
                vals = ctrl.get_status(axis)
                st = MotorStatus(vals[3])
                logger.info(f"{axis} 轴使能成功，当前状态: {st.name}")
            else:
                logger.error(f"{axis} 轴使能失败")
        except Exception as e:
            logger.error(f"{axis} 轴使能异常: {e}")
        time.sleep(0.5)


def test_status_read(ctrl: XYZStepperController):
    """
    读取各轴当前状态（调试）
    """
    logger.info("=== 当前各轴状态 ===")
    for axis in ["X", "Y", "Z"]:
        try:
            vals = ctrl.get_status(axis)
            st = MotorStatus(vals[3])
            logger.info(
                f"{axis}: steps={vals[0]}, speed={vals[1]}, "
                f"current={vals[2]}, status={st.name}"
            )
        except Exception as e:
            logger.error(f"获取 {axis} 状态失败: {e}")
        time.sleep(0.2)


def redefine_soft_zero(ctrl: XYZStepperController):
    """
    手动重新定义软零点
    """
    logger.info("=== ⚙️ 重新定义软零点 ===")
    ctrl.define_current_as_zero("work_origin.json")
    logger.info("✅ 新软零点已写入 work_origin.json")


def test_soft_zero_move(ctrl: XYZStepperController):
    """
    以软零点为基准执行三轴运动测试
    """
    logger.info("=== 测试软零点相对运动 ===")
    ctrl.move_xyz_work(x=100.0, y=100.0, z=40.0, speed=100, acc=800)

    for axis in ["X", "Y", "Z"]:
        ctrl.wait_complete(axis)

    test_status_read(ctrl)
    logger.info("✅ 软零点运动测试完成")


def main():
    ctrl = create_controller(port="/dev/ttyUSB1", baudrate=115200)

    try:
        test_enable_axis(ctrl)
        test_status_read(ctrl)

        # === 初始化或加载软零点 ===
        loaded = load_existing_soft_zero(ctrl)
        if not loaded:
            logger.info("👣 首次运行，定义软零点并保存。")
            ctrl.define_current_as_zero("work_origin.json")

        # === 软零点回归动作 ===
        ctrl.return_to_work_origin()

        # === 可选软零点运动测试 ===
        # test_soft_zero_move(ctrl)

    except KeyboardInterrupt:
        logger.info("🛑 手动中断退出")

    except Exception as e:
        logger.exception(f"❌ 调试出错: {e}")

    finally:
        if hasattr(ctrl.client, "transport"):
            ctrl.client.transport.close()
        logger.info("串口已安全关闭 ✅")


if __name__ == "__main__":
    main()
