
import logging
from xyz_stepper_driver import (
    ModbusRTUTransport,
    ModbusClient,
    XYZStepperController,
    MotorAxis,
)

logger = logging.getLogger("XYZStepperCommTest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def test_xyz_stepper_comm():
    """仅测试 Modbus 通信是否正常（并输出寄存器数据，不做电机运动）"""
    port = "/dev/ttyUSB1"
    baudrate = 115200
    timeout = 1.2  # 略长避免响应被截断

    logger.info(f"尝试连接 Modbus 设备 {port} ...")
    transport = ModbusRTUTransport(port, baudrate=baudrate, timeout=timeout)
    transport.open()

    client = ModbusClient(transport)
    ctrl = XYZStepperController(client)

    try:
        logger.info("✅ 串口已打开，开始读取三个轴状态（打印寄存器内容） ...")
        for axis in [MotorAxis.X, MotorAxis.Y, MotorAxis.Z]:
            addr = ctrl.axis_addr[axis]

            try:
                # # 在 get_status 前打印原始寄存器内容
                # regs = client.read_registers(addr, ctrl.REG_STATUS, 6)
                # hex_regs = [f"0x{val:04X}" for val in regs]
                # logger.info(f"[{axis.name}] 原始寄存器 ({len(regs)} 个): {regs} -> {hex_regs}")

                # 调用 get_status() 正常解析
                status = ctrl.get_status(axis)
                logger.info(
                    f"[{axis.name}] ✅ 通信正常: steps={status.steps}, speed={status.speed}, "
                    f"current={status.current}, status={status.status.name}"
                )

            except Exception as e_axis:
                logger.error(f"[{axis.name}] ❌ 通信失败: {e_axis}")


    except Exception as e:
        logger.error(f"❌ 通讯测试失败: {e}")

    finally:
        transport.close()
        logger.info("🔌 串口已关闭")


if __name__ == "__main__":
    test_xyz_stepper_comm()
