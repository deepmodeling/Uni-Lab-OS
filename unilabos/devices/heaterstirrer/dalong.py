import json
import serial
import time as systime


class HeaterStirrer_DaLong:
    def __init__(self, port: str = 'COM6', temp_warning=50.0, baudrate: int = 9600):
        try:
            self.serial = serial.Serial(port, baudrate, timeout=2)
        except serial.SerialException as e:
            print("串口错误", f"无法打开串口{port}: {e}")
        self._status = "Idle"
        self._stir_speed = 0.0
        self._temp = 0.0
        self._temp_warning = temp_warning
        self.set_temp_warning(temp_warning)
        self._temp_target = 20.0
        self.success = False

    @property
    def status(self) -> str:
        return self._status

    def get_status(self) -> str:
        self._status = "Idle" if self.stir_speed == 0 else "Running"

    @property
    def stir_speed(self) -> float:
        return self._stir_speed

    def set_stir_speed(self, speed: float):
        try:
            # 转换速度为整数
            speed_int = int(speed)
            # 确保速度在允许的范围内
            if speed_int < 0 or speed_int > 65535:
                raise ValueError("速度必须在0到65535之间")
        except ValueError as e:
            print("输入错误", str(e))
            return

        # 计算高位和低位
        speed_high = speed_int >> 8
        speed_low = speed_int & 0xFF

        # 构建搅拌控制指令
        command = bytearray([0xfe, 0xB1, speed_high, speed_low, 0x00])
        # 计算校验和
        command.append(sum(command[1:]) % 256)

        # 发送指令
        self.serial.write(command)
        # 检查响应
        response = self.serial.read(6)
        if len(response) == 6 and response[0] == 0xfd and response[1] == 0xB1 and response[2] == 0x00:
            print("成功", "搅拌速度更新成功")
            self._stir_speed = speed
        else:
            print("失败", "搅拌速度更新失败")

    def heatchill(
        self,
        vessel: str,
        temp: float,
        time: float = 3600,
        stir: bool = True,
        stir_speed: float = 300,
        purpose: str = "reaction"
    ):
        self.set_temp_target(temp)
        if stir:
            self.set_stir_speed(stir_speed)
            self.status = "Stirring"
            systime.sleep(time)
            self.set_stir_speed(0)
            self.status = "Idle"

    @property
    def temp(self) -> float:
        self._temp = self.get_temp()
        return self._temp

    def get_temp(self):
        # 构建读取温度的指令
        command = bytearray([0xfe, 0xA2, 0x00, 0x00, 0x00])
        command.append(sum(command[1:]) % 256)

        # 发送指令
        self.serial.write(command)
        # 读取响应
        systime.sleep(0.1)
        num_bytes = self.serial.in_waiting
        response = self.serial.read(num_bytes)
        try:
            high_value = response[8]
            low_value = response[9]
            raw_temp = (high_value << 8) + low_value
            if raw_temp & 0x8000:  # 如果低位寄存器最高位为1，表示负值
                raw_temp -= 0x10000  # 转换为正确的负数表示
            temp = raw_temp / 10
            return temp
        except:
            return None

    @property
    def temp_warning(self) -> float:
        return self._temp_warning

    def set_temp_warning(self, temp):
        self.success = False
        # temp = round(float(warning_temp), 1)
        if self.set_temp_inner(float(temp), "warning"):
            self._temp_warning = round(float(temp), 1)
            self.success = True

    @property
    def temp_target(self) -> float:
        return self._temp_target

    def set_temp_target(self, temp):
        self.success = False
        # temp = round(float(target_temp), 1)
        if self.set_temp_inner(float(temp), "target"):
            self._temp_target = round(float(temp), 1)
            self.success = True

    def set_temp_inner(self, temp: float, type: str = "warning"):
        try:
            # 转换为整数
            temp_int = int(temp*10)
        except ValueError as e:
            print("输入错误", str(e))
            return

        # 计算高位和低位
        temp_high = temp_int >> 8
        temp_low = temp_int & 0xFF

        # 构建控制指令
        if type == "warning":
            command = bytearray([0xfe, 0xB4, temp_high, temp_low, 0x00])
        elif type == "target":
            command = bytearray([0xfe, 0xB2, temp_high, temp_low, 0x00])
        else:
            return False
        # 计算校验和
        command.append(sum(command[1:]) % 256)
        print(command)
        # 发送指令
        self.serial.write(command)
        # 检查响应
        systime.sleep(0.1)
        response = self.serial.read(6)
        print(response)
        if len(response) == 6 and response[0] == 0xfd and response[1] == 0xB4 and response[2] == 0x00:
            print("成功", "安全温度设置成功")
            return True
        else:
            print("失败", "安全温度设置失败")
            return False

    def close(self):
        self.serial.close()


if __name__ == "__main__":
    import tkinter as tk
    from tkinter import messagebox

    heaterstirrer = HeaterStirrer_DaLong()
    # heaterstirrer.set_mix_speed(0)
    heaterstirrer.get_temp()
    # heaterstirrer.set_warning(17)
    print(heaterstirrer.temp)
    print(heaterstirrer.temp_warning)

    # 创建主窗口
    # root = tk.Tk()
    # root.title("搅拌速度控制")

    # # 创建速度变量
    # speed_var = tk.StringVar()

    # # 创建输入框
    # speed_entry = tk.Entry(root, textvariable=speed_var)
    # speed_entry.pack(pady=10)

    # # 创建按钮
    # set_speed_button = tk.Button(root, text="确定", command=heaterstirrer.set_mix_speed)
    # # set_speed_button = tk.Button(root, text="确定", command=heaterstirrer.read_temp)
    # set_speed_button.pack(pady=5)

    # # 运行主事件循环
    # root.mainloop()

    # 关闭串口
    heaterstirrer.serial.close()
