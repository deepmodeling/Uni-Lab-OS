import serial
import serial.tools.list_ports
import time
import sys

# 继电器控制指令
RELAY_ON = bytes.fromhex('A0 01 01 A2')  # 打开继电器
RELAY_OFF = bytes.fromhex('A0 01 00 A1')  # 关闭继电器
RELAY_ON_WITH_RESPONSE = bytes.fromhex('A0 01 03 A4')  # 打开继电器,带回复
RELAY_OFF_WITH_RESPONSE = bytes.fromhex('A0 01 02 A3')  # 关闭继电器,带回复

class RelayController:
    """USB继电器控制类

    参数:
        port: 串口名称, 如 'COM3'
        baudrate: 波特率, 默认9600
        timeout: 读超时秒数
    """
    def __init__(self, port: str = 'COM3', baudrate: int = 9600, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None
        self._state: bool | None = None  # True=ON False=OFF None=未知

    def connect(self):
        if self.ser and self.ser.is_open:
            return
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
            time.sleep(2)  # 等待设备初始化
        except Exception as e:
            raise RuntimeError(f'连接串口失败: {e}')

    def _write(self, data: bytes, expect_response: bool = True) -> bytes | None:
        if not self.ser or not self.ser.is_open:
            raise RuntimeError('串口未连接, 请先调用 connect()')
        self.ser.write(data)
        if expect_response:
            # 读取直到最后一个字节或超时
            end_byte = data[-1:]
            resp = self.ser.read_until(end_byte)
            return resp
        return None

    def on(self, wait_response: bool = True):
        resp = self._write(RELAY_ON_WITH_RESPONSE if wait_response else RELAY_ON, expect_response=wait_response)
        self._state = True
        return resp

    def off(self, wait_response: bool = True):
        resp = self._write(RELAY_OFF_WITH_RESPONSE if wait_response else RELAY_OFF, expect_response=wait_response)
        self._state = False
        return resp

    def toggle(self, wait_response: bool = True):
        if self._state is None:
            # 未知状态默认先关闭再打开
            self.off(wait_response)
            time.sleep(0.1)
            return self.on(wait_response)
        return self.off(wait_response) if self._state else self.on(wait_response)

    def state(self) -> bool | None:
        return self._state

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def ensure_off_on_exit(self):
        # 安全关闭
        try:
            self.off(wait_response=False)
        except Exception:
            pass
        finally:
            self.close()

# 示例入口
if __name__ == '__main__':
    print('=' * 40)
    print('        USB继电器控制程序')
    print('=' * 40)
    run_time_input = input('输入运行时间(秒): ').strip()
    try:
        run_seconds = float(run_time_input)
    except ValueError:
        print('输入不是数字, 退出.')
        sys.exit(1)

    controller = RelayController(port='COM7')
    try:
        controller.connect()
        controller.on()  # 初始化关闭
        print('打开搅拌器') 
        print(f'等待 {run_seconds} 秒后关闭...')
        time.sleep(run_seconds)
        controller.off()
        print('关闭搅拌器')
    except Exception as e:
        print('发生错误:', e)
    finally:
        controller.ensure_off_on_exit()
        print('程序结束')