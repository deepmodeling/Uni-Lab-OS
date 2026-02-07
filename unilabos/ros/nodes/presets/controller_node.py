from typing import Callable, Dict
from std_msgs.msg import Float64

from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, DeviceNodeResourceTracker


class ControllerNode(BaseROS2DeviceNode):
    namespace_prefix = "/controllers"

    def __init__(
        self,
        device_id: str,
        registry_name: str,
        controller_func: Callable,
        update_rate: float,
        inputs: Dict[str, Dict[str, type | str]],
        outputs: Dict[str, Dict[str, type]],
        parameters: Dict,
        resource_tracker: DeviceNodeResourceTracker,
    ):
        """
        通用控制器节点

        :param controller_id: 控制器的唯一标识符（作为命名空间的一部分）
        :param update_rate: 控制器更新频率 (Hz)
        :param controller_func: 控制器函数，接收 Python 格式的 inputs 和 parameters 返回 outputs
        :param input_types: 输入话题及其消息类型的字典
        :param output_types: 输出话题及其消息类型的字典
        :param parameters: 控制器函数的额外参数
        """
        # 先准备所需的属性，以便在调用父类初始化前就可以使用
        self.device_id = device_id
        self.controller_func = controller_func
        self.update_rate = update_rate
        self.update_time = 1.0 / update_rate
        self.parameters = parameters
        self.inputs = {topic: None for topic in inputs.keys()}
        self.control_input_subscribers = {}
        self.control_output_publishers = {}
        self.topic_mapping = {
            **{input_info["topic"]: input for input, input_info in inputs.items()},
            **{output_info["topic"]: output for output, output_info in outputs.items()},
        }

        # 调用BaseROS2DeviceNode初始化，使用自身作为driver_instance
        status_types = {}
        action_value_mappings = {}
        hardware_interface = {}

        # 使用短ID作为节点名，完整ID（带namespace_prefix）作为device_id
        BaseROS2DeviceNode.__init__(
            self,
            driver_instance=self,
            device_id=device_id,
            registry_name=registry_name,
            status_types=status_types,
            action_value_mappings=action_value_mappings,
            hardware_interface=hardware_interface,
            print_publish=False,
            resource_tracker=resource_tracker
        )

        # 原始初始化逻辑
        # 初始化订阅者
        for input, input_info in inputs.items():
            msg_type = input_info["type"]
            topic = str(input_info["topic"])
            self.control_input_subscribers[input] = self.create_subscription(
                msg_type, topic, lambda msg, t=topic: self.input_callback(t, msg), 10
            )

        # 初始化发布者
        for output, output_info in outputs.items():
            self.lab_logger().info(f"Creating publisher for {output} {output_info}")
            msg_type = output_info["type"]
            topic = str(output_info["topic"])
            self.control_output_publishers[output] = self.create_publisher(msg_type, topic, 10)

        # 定时器，用于定期调用控制逻辑
        self.timer = self.create_timer(self.update_time, self.control_loop)

    def input_callback(self, topic: str, msg):
        """
        更新指定话题的输入数据，并将 ROS 消息转换为普通 Python 数据。
        支持 `std_msgs` 类型消息。
        """
        self.inputs[self.topic_mapping[topic]] = msg.data
        self.lab_logger().info(f"Received input on topic {topic}: {msg.data}")

    def control_loop(self):
        """主控制逻辑"""
        # 检查所有输入是否已更新
        if all(value is not None for value in self.inputs.values()):
            self.lab_logger().info(
                f"Calling controller function with inputs: {self.inputs}, parameters: {self.parameters}"
            )
            try:
                # 调用控制器函数，传入 Python 格式的数据
                outputs = self.controller_func(**self.inputs, **self.parameters)
                self.lab_logger().info(f"Inputs: {self.inputs}, Outputs: {outputs}")
                self.inputs = {topic: None for topic in self.inputs.keys()}
            except Exception as e:
                self.lab_logger().error(f"Controller function execution failed: {e}")
                return

            # 发布控制信号，将普通 Python 数据转换为 ROS 消息
            if isinstance(outputs, dict):
                for topic, value in outputs.items():
                    if topic in self.control_output_publishers:
                        # 支持 Float64 输出
                        if isinstance(value, (float, int)):
                            self.control_output_publishers[topic].publish(Float64(data=value))
                        else:
                            self.lab_logger().error(f"Unsupported output type for topic {topic}: {type(value)}")
                    else:
                        self.lab_logger().warning(f"Output topic {topic} is not defined in output_types.")
            else:
                publisher = list(self.control_output_publishers.values())[0]
                if isinstance(outputs, (float, int)):
                    publisher.publish(Float64(data=outputs))
                else:
                    self.lab_logger().error(f"Unsupported output type: {type(outputs)}")
        else:
            self.lab_logger().info("Waiting for all inputs to be received.")
