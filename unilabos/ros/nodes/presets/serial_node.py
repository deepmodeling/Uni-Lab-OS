from threading import Lock

from unilabos_msgs.srv import SerialCommand
from serial import Serial, SerialException

from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, DeviceNodeResourceTracker


class ROS2SerialNode(BaseROS2DeviceNode):
    def __init__(self, device_id, registry_name, port: str, baudrate: int = 9600, resource_tracker: DeviceNodeResourceTracker=None):
        # 保存属性，以便在调用父类初始化前使用
        self.port = port
        self.baudrate = baudrate
        self._hardware_interface = {"name": "hardware_interface", "write": "send_command", "read": "read_data"}
        self._busy = False
        self._closing = False
        self._query_lock = Lock()

        # 初始化硬件接口
        try:
            self.hardware_interface = Serial(baudrate=baudrate, port=port)
        except (OSError, SerialException) as e:
            # 因为还没调用父类初始化，无法使用日志，直接抛出异常
            # print(f"Failed to connect to serial port {port} at {baudrate} baudrate.")
            raise RuntimeError(f"Failed to connect to serial port {port} at {baudrate} baudrate.") from e

        # 初始化BaseROS2DeviceNode，使用自身作为driver_instance
        BaseROS2DeviceNode.__init__(
            self,
            driver_instance=self,
            registry_name=registry_name,
            device_id=device_id,
            status_types={},
            action_value_mappings={},
            hardware_interface=self._hardware_interface,
            print_publish=False,
            resource_tracker=resource_tracker,
        )

        # 现在可以使用日志
        self.lab_logger().info(
            f"【ROS2SerialNode.__init__】初始化串口节点: {device_id}, 端口: {port}, 波特率: {baudrate}"
        )
        self.lab_logger().info(f"【ROS2SerialNode.__init__】成功连接串口设备")

        # 创建服务
        self.create_service(SerialCommand, "serialwrite", self.handle_serial_request)
        self.lab_logger().info(f"【ROS2SerialNode.__init__】创建串口写入服务: serialwrite")

    def send_command(self, command: str):
        # self.lab_logger().debug(f"【ROS2SerialNode.send_command】发送命令: {command}")
        with self._query_lock:
            if self._closing:
                self.lab_logger().error(f"【ROS2SerialNode.send_command】设备正在关闭，无法发送命令")
                raise RuntimeError

            full_command = f"{command}\n"
            full_command_data = bytearray(full_command, "ascii")

            response = self.hardware_interface.write(full_command_data)
            # time.sleep(0.05)
            output = self._receive(self.hardware_interface.read_until(b"\n"))
            # self.lab_logger().debug(f"【ROS2SerialNode.send_command】接收响应: {output}")
        return output

    def read_data(self):
        # self.lab_logger().debug(f"【ROS2SerialNode.read_data】读取数据")
        with self._query_lock:
            if self._closing:
                self.lab_logger().error(f"【ROS2SerialNode.read_data】设备正在关闭，无法读取数据")
                raise RuntimeError
            data = self.hardware_interface.read_until(b"\n")
            result = self._receive(data)
            # self.lab_logger().debug(f"【ROS2SerialNode.read_data】读取到数据: {result}")
            return result

    def _receive(self, data: bytes):
        ascii_string = "".join(chr(byte) for byte in data)
        # self.lab_logger().debug(f"【ROS2SerialNode._receive】接收数据: {ascii_string}")
        return ascii_string

    def handle_serial_request(self, request, response):
        self.lab_logger().info(f"【ROS2SerialNode.handle_serial_request】收到串口命令请求: {request.command}")
        response.response = self.send_command(request.command)
        self.lab_logger().info(f"【ROS2SerialNode.handle_serial_request】命令响应: {response.response}")
        return response
