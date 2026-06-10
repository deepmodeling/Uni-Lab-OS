import socket
import json
import time
from pydantic import BaseModel


class AgvNavigator:
    def __init__(self, host):
        self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.receive_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.control_socket.connect((host, 19206))
        self.receive_socket.connect((host, 19204))
        self.rec_cmd_code = {
            "pose": "03EC",
            "status": "03FC",
            "nav": "0BEB"
        }
        self.status_list = ['NONE', 'WAITING', 'RUNNING', 'SUSPENDED', 'COMPLETED', 'FAILED', 'CANCELED']
        self._pose = []
        self._status = 'NONE'
        self.success = False

    @property
    def pose(self) -> list:
        data = self.send('pose')

        try:
            self._pose = [data['x'], data['y'], data['angle']]
        except:
            print(data)

        return self._pose

    @property
    def status(self) -> str:
        data = self.send('status')
        self._status = self.status_list[data['task_status']]
        return self._status

    def send(self, cmd, ex_data='', obj='receive_socket'):
        data = bytearray.fromhex(f"5A 01 00 01 00 00 00 00 {self.rec_cmd_code[cmd]} 00 00 00 00 00 00")
        if ex_data:
            data_ = ex_data
            data[7] = len(data_)
            data = data + bytearray(data_, "utf-8")

        cmd_obj = getattr(self, obj)
        cmd_obj.sendall(data)
        response_data = b""
        while True:
            part = cmd_obj.recv(4096)  # 每次接收 4096 字节
            response_data += part
            if len(part) < 4096:  # 当接收到的数据少于缓冲区大小时，表示接收完毕
                break

        response_str = response_data.hex()
        json_start = response_str.find('7b')  # 找到JSON的开始位置
        if json_start == -1:
            raise "Error: No JSON data found in response."

        json_data = bytes.fromhex(response_str[json_start:])

        # 尝试解析 JSON 数据
        try:
            parsed_json = json.loads(json_data.decode('utf-8'))
            return parsed_json

        except json.JSONDecodeError as e:
            raise f"JSON Decode Error: {e}"

    def send_nav_task(self, command: str):
        self.success = False
        # source,target = cmd.replace(' ','').split("->")

        target = json.loads(command)['target']
        json_data = {}
        # json_data["source_id"] = source
        json_data["id"] = target
        # json_data["use_down_pgv"] = True
        result = self.send('nav', ex_data=json.dumps(json_data), obj="control_socket")
        try:
            if result['ret_code'] == 0:
                # print(result)
                while True:
                    if self.status == 'COMPLETED':
                        break
                    time.sleep(1)
                self.success = True
        except:
            self.success = False

    def __del__(self):
        self.control_socket.close()
        self.receive_socket.close()


if __name__ == "__main__":
    agv = AgvNavigator("192.168.1.42")
    # print(agv.pose)
    agv.send_nav_task('LM14')
