import serial
import time

class SealRemoverController:
    def __init__(self, port='COM17', baudrate=9600, timeout=2):
        self.ser = serial.Serial(port, baudrate, timeout=timeout)

    def send_command(self, command):
        full_cmd = f"{command}\r\n".encode('ascii')
        self.ser.write(full_cmd)
        time.sleep(0.5)  # 稍等设备响应
        return self.read_response()

    def read_response(self):
        lines = []
        while self.ser.in_waiting:
            line = self.ser.readline().decode('ascii').strip()
            lines.append(line)
        return lines

    def reset(self):
        return self.send_command("*reset")

    def restart(self):
        return self.send_command("*restart")

    def check_status(self):
        return self.send_command("*stat")

    def move_in(self):
        return self.send_command("*movein")

    def move_out(self):
        return self.send_command("*moveout")

    def move_up(self):
        return self.send_command("*moveup")

    def move_down(self):
        return self.send_command("*movedown")

    def peel(self, param_set=1, adhere_time=1):
        # param_set: 1~9, adhere_time: 1(2.5s) ~ 4(10s)
        return self.send_command(f"*xpeel:{param_set}{adhere_time}")

    def check_tape(self):
        return self.send_command("*tapeleft")

    def close(self):
        self.ser.close()

if __name__ == "__main__":
    remover = SealRemoverController(port='COM17') 
    remover.restart # "restart"
    remover.check_status() # "检查状态:"
    remover.reset() # 复位设备
    remover.peel(param_set=4, adhere_time=1) #执行撕膜操作,慢速+2.5s
    remover.move_out() # 送出板子
    remover.close()