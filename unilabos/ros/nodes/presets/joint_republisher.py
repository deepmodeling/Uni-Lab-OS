import json
import sys
import uuid

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import String

from unilabos.ros.logging import close_ros_logging, prepare_ros_logging
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class JointRepublisher(BaseROS2DeviceNode):
    def __init__(self,device_id, registry_name, resource_tracker, **kwargs):
        super().__init__(
            driver_instance=self,
            device_id=device_id,
            registry_name=registry_name,
            status_types={},
            action_value_mappings={},
            hardware_interface={},
            print_publish=False,
            resource_tracker=resource_tracker,  
            device_uuid=kwargs.get("uuid", str(uuid.uuid4())),
        )  
        
        # print('-'*20,device_id)
        self.joint_repub = self.create_publisher(String,f'joint_state_repub',10)
        # 创建订阅者
        self.create_subscription(
            JointState,               
            '/joint_states',         
            self.listener_callback,  
            10,
            callback_group=ReentrantCallbackGroup()
        )
        self.msg = String()

    def listener_callback(self, msg:JointState):
        
        try:
            json_dict = {}
            json_dict["name"]           = list(msg.name)
            json_dict["position"]       = list(msg.position)
            json_dict["velocity"]       = list(msg.velocity)
            json_dict["effort"]         = list(msg.effort)

            self.msg.data = str(json_dict)
            self.joint_repub.publish(self.msg)
            # print('-'*20)
            # print(self.msg.data)
            
        except Exception as e:
            print(e)


def main():

    rclpy.init(args=prepare_ros_logging(sys.argv, context_initialized=rclpy.ok()))
    subscriber = JointRepublisher()
    try:
        rclpy.spin(subscriber)
    finally:
        subscriber.destroy_node()
        try:
            rclpy.shutdown()
        finally:
            close_ros_logging()


if __name__ == '__main__':
    main()
