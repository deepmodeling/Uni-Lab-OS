import asyncio
import copy
from pathlib import Path
import threading
import uuid
import rclpy
import json
import time
from rclpy.executors import MultiThreadedExecutor
from rclpy.action import ActionServer,ActionClient
from sensor_msgs.msg import JointState
from unilabos_msgs.action import SendCmd
from rclpy.action.server import ServerGoalHandle
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from tf_transformations import quaternion_from_euler
from tf2_ros import TransformBroadcaster, Buffer, TransformListener 

from rclpy.node import Node
import re

class LiquidHandlerJointPublisher(BaseROS2DeviceNode):
    def __init__(self,resources_config:list, resource_tracker, rate=50, device_id:str = "lh_joint_publisher", **kwargs):
        super().__init__(
            driver_instance=self,
            device_id=device_id,
            status_types={},
            action_value_mappings={},
            hardware_interface={},
            print_publish=False,
            resource_tracker=resource_tracker,  
            device_uuid=kwargs.get("uuid", str(uuid.uuid4())),
        )  
        
        # 初始化参数
        self.j_msg          = JointState()
        joint_config        = json.load(open(f"{Path(__file__).parent.absolute()}/lh_joint_config.json", encoding="utf-8"))
        self.resources_config = {x['id']:x for x in resources_config}
        self.rate           = rate
        self.tf_buffer      = Buffer()
        self.tf_listener    = TransformListener(self.tf_buffer, self)
        self.j_pub          = self.create_publisher(JointState,'/joint_states',10)
        self.create_timer(1,self.lh_joint_pub_callback)


        self.resource_action = None
        
        while self.resource_action is None:
            self.resource_action = self.check_tf_update_actions()
            time.sleep(1)
        
        self.resource_action_client = ActionClient(self, SendCmd, self.resource_action)
        while not self.resource_action_client.wait_for_server(timeout_sec=1.0):
            self.get_logger().info('等待 TfUpdate 服务器...')

        self.deck_list = []
        self.lh_devices = {}
        # 初始化设备ID与config信息
        for resource in resources_config:
            if resource['class'] == 'liquid_handler':
                deck_id = resource['config']['deck']['_resource_child_name']
                deck_class = resource['config']['deck']['_resource_type'].split(':')[-1]
                key = f'{deck_id}'
                # key = f'{resource["id"]}_{deck_id}'
                self.lh_devices[key] = {
                    'joint_msg':JointState(
                        name=[f'{key}_{x}' for x in joint_config[deck_class]['joint_names']],
                        position=[0.0 for _ in joint_config[deck_class]['joint_names']]
                        ),
                    'joint_config':joint_config[deck_class]
                }
                self.deck_list.append(deck_id)


        self.j_action       = ActionServer(
            self,
            SendCmd,
            "hl_joint_action",
            self.lh_joint_action_callback,
            result_timeout=5000
        )

                
    def check_tf_update_actions(self):
        topics = self.get_topic_names_and_types()

        
        for topic_item in topics:

            topic_name, topic_types = topic_item

            if 'action_msgs/msg/GoalStatusArray' in topic_types:
                # 删除 /_action/status 部分

                base_name = topic_name.replace('/_action/status', '')
                # 检查最后一个部分是否为 tf_update
                parts = base_name.split('/')
                if parts and parts[-1] == 'tf_update':
                    return base_name
                
        return None
    

    def find_resource_parent(self, resource_id:str):
        # 遍历父辈，找到父辈的父辈，直到找到设备ID
        parent_id = self.resources_config[resource_id]['parent']
        try:
            if parent_id in self.deck_list:
                p_ = self.resources_config[parent_id]['parent']
                str_ = f'{parent_id}'
                return str(str_)
            else:
                return self.find_resource_parent(parent_id)
        except Exception as e:
            return None


    def send_resource_action(self, resource_id_list:list[str], link_name:str):
        goal_msg = SendCmd.Goal()
        str_dict = {}
        for resource in resource_id_list:
            str_dict[resource] = link_name

        goal_msg.command = json.dumps(str_dict)
        self.resource_action_client.send_goal(goal_msg)
    
    def resource_move(self, resource_id:str, link_name:str, channels:list[int]):
        resource = resource_id.rsplit("_",1)
        
        channel_list = ['A','B','C','D','E','F','G','H']

        resource_list = []
        match = re.match(r'([a-zA-Z_]+)(\d+)', resource[1])
        if match:
            number = match.group(2)
            for channel in channels:
                resource_list.append(f"{resource[0]}_{channel_list[channel]}{number}")

        if len(resource_list) > 0:
            self.send_resource_action(resource_list, link_name)



    def lh_joint_action_callback(self,goal_handle: ServerGoalHandle):
        """Move a single joint

        Args:
            command: A JSON-formatted string that includes joint_name, speed, position

                    joint_name (str): The name of the joint to move
                    speed (float): The speed of the movement, speed > 0
                    position (float): The position to move to

        Returns:
            None
        """
        result = SendCmd.Result()
        cmd_str = str(goal_handle.request.command).replace('\'','\"')
        # goal_handle.execute()

        try:
            cmd_dict = json.loads(cmd_str)
            self.move_joints(**cmd_dict)
            result.success = True
            goal_handle.succeed()
            
        except Exception as e:
            print(f'Liquid handler action error: \n{e}')
            goal_handle.abort()
            result.success = False
        
        return result
    def inverse_kinematics(self, x, y, z, 
                           parent_id,
                           x_joint:dict, 
                           y_joint:dict, 
                           z_joint:dict   ):
        """
        将x、y、z坐标转换为对应关节的位置
        
        Args:
            x (float): x坐标
            y (float): y坐标
            z (float): z坐标
            x_joint (dict): x轴关节配置，包含factor和offset
            y_joint (dict): y轴关节配置，包含factor和offset
            z_joint (dict): z轴关节配置，包含factor和offset
            
        Returns:
            dict: 关节名称和对应位置的字典
        """
        joint_positions = copy.deepcopy(self.lh_devices[parent_id]['joint_msg'].position)
        
        z_index = 0
        # 处理x轴关节
        for joint_name, config in x_joint.items():
            index = self.lh_devices[parent_id]['joint_msg'].name.index(f"{parent_id}_{joint_name}")
            joint_positions[index] = x * config["factor"] + config["offset"]
            
        # 处理y轴关节
        for joint_name, config in y_joint.items():
            index = self.lh_devices[parent_id]['joint_msg'].name.index(f"{parent_id}_{joint_name}")
            joint_positions[index] = y * config["factor"] + config["offset"]
            
        # 处理z轴关节
        for joint_name, config in z_joint.items():
            index = self.lh_devices[parent_id]['joint_msg'].name.index(f"{parent_id}_{joint_name}")
            joint_positions[index] = z * config["factor"] + config["offset"]
            z_index = index

        return joint_positions ,z_index


    def move_joints(self, resource_names, x, y, z, option, speed = 0.1 ,x_joint=None, y_joint=None, z_joint=None,channels=[0,1,2,3,4,5,6,7]):
        if isinstance(resource_names, list):
            resource_name_ = resource_names[0]
        else:
            resource_name_ = resource_names
        
        parent_id = self.find_resource_parent(resource_name_)


        # print('!'*20)
        # print(parent_id)
        # print('!'*20)
        if x_joint is None:
            xa,xb = next(iter(self.lh_devices[parent_id]['joint_config']['x'].items()))
            x_joint_config = {xa:xb}
        elif x_joint in self.lh_devices[parent_id]['joint_config']['x']:
            x_joint_config = self.lh_devices[parent_id]['joint_config']['x'][x_joint]
        else:
            raise ValueError(f"x_joint {x_joint} not in joint_config['x']")
        if y_joint is None:
            ya,yb = next(iter(self.lh_devices[parent_id]['joint_config']['y'].items()))
            y_joint_config = {ya:yb}
        elif y_joint in self.lh_devices[parent_id]['joint_config']['y']:
            y_joint_config = self.lh_devices[parent_id]['joint_config']['y'][y_joint]
        else:
            raise ValueError(f"y_joint {y_joint} not in joint_config['y']")
        if z_joint is None:
            za, zb = next(iter(self.lh_devices[parent_id]['joint_config']['z'].items()))
            z_joint_config = {za :zb}
        elif z_joint in self.lh_devices[parent_id]['joint_config']['z']:
            z_joint_config = self.lh_devices[parent_id]['joint_config']['z'][z_joint]
        else:
            raise ValueError(f"z_joint {z_joint} not in joint_config['z']")

        joint_positions_target, z_index = self.inverse_kinematics(x,y,z,parent_id,x_joint_config,y_joint_config,z_joint_config)
        joint_positions_target_zero = copy.deepcopy(joint_positions_target)
        joint_positions_target_zero[z_index] = 0

        self.move_to(joint_positions_target_zero, speed, parent_id)
        self.move_to(joint_positions_target, speed, parent_id)
        time.sleep(1)
        if option == "pick":
            link_name =  self.lh_devices[parent_id]['joint_config']['link_names'][z_index]
            link_name =  f'{parent_id}_{link_name}'
            self.resource_move(resource_name_, link_name, channels)
        elif option == "drop_trash":
            self.resource_move(resource_name_, "__trash", channels)
        elif option == "drop":
            self.resource_move(resource_name_, "world", channels)
        self.move_to(joint_positions_target_zero, speed, parent_id)


    def move_to(self, joint_positions ,speed, parent_id):
        loop_flag = 0

        while loop_flag < len(joint_positions):
            loop_flag = 0
            for i in range(len(joint_positions)):
                distance = joint_positions[i] - self.lh_devices[parent_id]['joint_msg'].position[i]
                if distance == 0:
                    loop_flag += 1
                    continue
                minus_flag = distance/abs(distance)
                if abs(distance) > speed/self.rate:
                    self.lh_devices[parent_id]['joint_msg'].position[i] += minus_flag * speed/self.rate
                else :
                    self.lh_devices[parent_id]['joint_msg'].position[i] = joint_positions[i]
                    loop_flag += 1
                    

            # 发布关节状态
            self.lh_joint_pub_callback()
            time.sleep(1/self.rate)

    def lh_joint_pub_callback(self):
        for id, config in self.lh_devices.items():
            config['joint_msg'].header.stamp = self.get_clock().now().to_msg()
            self.j_pub.publish(config['joint_msg'])




class JointStatePublisher(Node):
    def __init__(self):
        super().__init__('joint_state_publisher')

        self.lh_action = None
        
        while self.lh_action is None:
            self.lh_action = self.check_hl_joint_actions()
            time.sleep(1)
        
        self.lh_action_client = ActionClient(self, SendCmd, self.lh_action)
        while not self.lh_action_client.wait_for_server(timeout_sec=1.0):
            self.get_logger().info('等待 TfUpdate 服务器...')

        
        
    def check_hl_joint_actions(self):
        topics = self.get_topic_names_and_types()

        
        for topic_item in topics:

            topic_name, topic_types = topic_item

            if 'action_msgs/msg/GoalStatusArray' in topic_types:
                # 删除 /_action/status 部分

                base_name = topic_name.replace('/_action/status', '')
                # 检查最后一个部分是否为 tf_update
                parts = base_name.split('/')
                if parts and parts[-1] == 'hl_joint_action':
                    return base_name
                
        return None
    
    def send_resource_action(self, resource_name, x,y,z,option, speed = 0.1,x_joint=None, y_joint=None, z_joint=None,channels=[0,1,2,3,4,5,6,7]):
        goal_msg = SendCmd.Goal()

        # Convert numpy arrays or other non-serializable objects to lists
        def to_serializable(obj):
            if hasattr(obj, 'tolist'):  # numpy array
                return obj.tolist()
            elif isinstance(obj, list):
                return [to_serializable(item) for item in obj]
            elif isinstance(obj, dict):
                return {k: to_serializable(v) for k, v in obj.items()}
            else:
                return obj

        str_dict = {
            'resource_names':resource_name,
            'x':x,
            'y':y,
            'z':z,
            'option':option,
            'speed':speed,
            'x_joint':to_serializable(x_joint),
            'y_joint':to_serializable(y_joint),
            'z_joint':to_serializable(z_joint),
            'channels':to_serializable(channels)
        }
        

        goal_msg.command = json.dumps(str_dict)

        if not self.lh_action_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error('Action server not available')
            return None
        
        try:
            # 创建新的executor
            executor = rclpy.executors.MultiThreadedExecutor()
            executor.add_node(self)
            
            # 发送目标
            future = self.lh_action_client.send_goal_async(goal_msg)
            
            # 使用executor等待结果
            while not future.done():
                executor.spin_once(timeout_sec=0.1)
            
            handle = future.result()
            
            if not handle.accepted:
                self.get_logger().error('Goal was rejected')
                return None
                
            # 等待最终结果
            result_future = handle.get_result_async()
            while not result_future.done():
                executor.spin_once(timeout_sec=0.1)
                
            result = result_future.result()
            return result
            
        except Exception as e:
            self.get_logger().error(f'Error during action execution: {str(e)}')
            return None
        finally:
            # 清理executor
            executor.remove_node(self)


def main():

    pass

if __name__ == '__main__':
    main()