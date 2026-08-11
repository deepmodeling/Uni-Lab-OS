import socket
import re
import time
from sensor_msgs.msg import JointState


class EliteRobot:
    def __init__(self,device_id, host, **kwargs):
        self.host = host
        self.node = None
        self.joint_state_msg = JointState()
        self.joint_state_msg.name = [f"{device_id}_shoulder_pan_joint",
                                     f"{device_id}_shoulder_lift_joint", 
                                     f"{device_id}_elbow_joint", 
                                     f"{device_id}_wrist_1_joint", 
                                     f"{device_id}_wrist_2_joint", 
                                     f"{device_id}_wrist_3_joint"]
        
        self.job_id = 0
        self.joint_state_pub = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 实现一个简单的Modbus TCP/IP协议客户端，端口为502
        self.modbus_port = 502
        self.modbus_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.modbus_sock.connect((self.host, self.modbus_port))
            print(f"已成功连接到Modbus服务器 {self.host}:{self.modbus_port}")
        except Exception as e:
            print(f"连接到Modbus服务器 {self.host}:{self.modbus_port} 失败: {e}")

        try:
            self.sock.connect((self.host, 40011))
            print(f"已成功连接到 {self.host}:{40011}")
        except Exception as e:
            print(f"连接到 {self.host}:{40011} 失败: {e}")

    def post_init(self, node):
        """使用当前 backend 提供的通用节点创建关节状态发布者。"""

        self.node = node
        self.joint_state_pub = node.create_publisher(
            JointState,
            "/joint_states",
            10,
        )

    def modbus_close(self):
        self.modbus_sock.close()
        

    @property
    def arm_pose(self) -> list[float]:
        return self.get_actual_joint_positions()
    

    def modbus_write_single_register(self, unit_id, register_addr, value):
        """
        写入单个Modbus保持寄存器（带符号整数）
        :param unit_id: 从站地址
        :param register_addr: 寄存器地址
        :param value: 要写入的值（-32768~32767，带符号16位整数）
        :return: True表示写入成功，False表示失败
        """
        transaction_id = 0x0001
        protocol_id = 0x0000
        length = 6  # 后续字节数
        function_code = 0x06  # 写单个保持寄存器
        header = transaction_id.to_bytes(2, 'big') + protocol_id.to_bytes(2, 'big') + length.to_bytes(2, 'big')
        # value用带符号16位整数编码
        body = (
            unit_id.to_bytes(1, 'big') +
            function_code.to_bytes(1, 'big') +
            register_addr.to_bytes(2, 'big') +
            value.to_bytes(2, 'big', signed=True)
        )
        request = header + body
        try:
            self.modbus_sock.sendall(request)
            response = self.modbus_sock.recv(1024)
            # 响应应与请求的body部分一致
            if len(response) >= 12 and response[7] == function_code:
                # 响应的寄存器值也要按带符号整数比对
                if response[8:12] == body[2:]:
                    return True
                else:
                    print("Modbus写入响应内容与请求不一致")
                    return False
            else:
                print("Modbus写入响应格式错误或功能码不匹配")
                return False
        except Exception as e:
            print("Modbus写入寄存器时出错:", e)
            return False

    def modbus_read_holding_registers(self, unit_id, start_addr, quantity):
        """
        读取Modbus保持寄存器（带符号整数）
        :param unit_id: 从站地址
        :param start_addr: 起始寄存器地址
        :param quantity: 读取数量
        :return: 读取到的数据（list of int，带符号16位整数），或None
        """
        # 构造Modbus TCP帧
        transaction_id = 0x0001
        protocol_id = 0x0000
        length = 6  # 后续字节数
        function_code = 0x03  # 读保持寄存器
        header = transaction_id.to_bytes(2, 'big') + protocol_id.to_bytes(2, 'big') + length.to_bytes(2, 'big')
        body = (
            unit_id.to_bytes(1, 'big') +
            function_code.to_bytes(1, 'big') +
            start_addr.to_bytes(2, 'big') +
            quantity.to_bytes(2, 'big')
        )
        request = header + body
        try:
            self.modbus_sock.sendall(request)
            response = self.modbus_sock.recv(1024)
            # 简单解析响应
            if len(response) >= 9 and response[7] == function_code:
                byte_count = response[8]
                data_bytes = response[9:9+byte_count]
                # 按带符号16位整数解码
                result = []
                for i in range(0, len(data_bytes), 2):
                    val = int.from_bytes(data_bytes[i:i+2], 'big', signed=True)
                    result.append(val)
                return result
            else:
                print("Modbus响应格式错误或功能码不匹配")
                return None
        except Exception as e:
            print("Modbus读取寄存器时出错:", e)
            return None

    def modbus_task(self, job_id):
        self.modbus_write_single_register(1, 256, job_id)
        self.job_id = job_id
        job = self.modbus_read_holding_registers(1, 257, 1)[0]
        while job != self.job_id:
            time.sleep(0.1)
            job = self.modbus_read_holding_registers(1, 257, 1)[0]
            self.get_actual_joint_positions()

            
    def modbus_task_cmd(self, command):

        if command == "lh2hplc":
            self.modbus_task(1)
            self.modbus_task(2)

        elif command == "hplc2lh":
            self.modbus_task(3)
            self.modbus_task(4)
            self.modbus_task(0)
        
    def send_command(self, command):
        self.sock.sendall(command.encode('utf-8'))
        response = self.sock.recv(1024).decode('utf-8')
        return response

    def close(self):
        self.sock.close()

    def __del__(self):
        self.close()

    def parse_success_response(self, response):
        """
        解析以[success]开头的返回数据，提取数组
        :param response: 字符串，形如 "[success] : [[3.158915, -1.961981, ...]]"
        :return: 数组（list of float），如果解析失败则返回None
        """
        if response.startswith("[1] [success]"):
            match = re.search(r"\[\[([^\]]+)\]\]", response)
            if match:
                data_str = match.group(1)
                try:
                    data_array = [float(x.strip()) for x in data_str.split(',')]
                    return data_array
                except Exception as e:
                    print("解析数组时出错:", e)
                    return None
        return None

    def get_actual_joint_positions(self):
        response = self.send_command(f"req 1 get_actual_joint_positions()\n")
        joint_positions = self.parse_success_response(response)
        if joint_positions:
            self.joint_state_msg.position = joint_positions
            self.joint_state_pub.publish(self.joint_state_msg)
            return joint_positions
        return None



if __name__ == "__main__":
    import rclpy
    from rclpy.node import Node

    rclpy.init()
    client = EliteRobot('aa',"192.168.1.200")
    client.post_init(Node("aa"))
    print(client.parse_success_response(client.send_command("req 1 get_actual_joint_positions()\n")))
    client.modbus_write_single_register(1, 256, 4)
    print(client.modbus_read_holding_registers(1, 257, 1))
    client.close()
