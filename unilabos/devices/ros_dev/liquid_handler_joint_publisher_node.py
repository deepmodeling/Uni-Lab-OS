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


from rclpy.node import Node
import re

class LiquidHandlerJointPublisher(Node):
    def __init__(self, joint_config:str = None, lh_device_id: str = 'lh_joint_publisher', rate=50, **kwargs):
        super().__init__(lh_device_id)
        # 初始化参数
        self.lh_device_id      = lh_device_id
        # INSERT_YOUR_CODE
        # 如果未传 joint_config，则自动读取同级的 lh_joint_config.json 文件

        config_path = Path(__file__).parent / 'lh_joint_config.json'
        with open(config_path, 'r', encoding='utf-8') as f:
            config_json = json.load(f)
        self.joint_config = config_json[joint_config]
        self.simulate_rviz = kwargs.get("simulate_rviz", False)


        self.rate           = rate
        self.j_pub          = self.create_publisher(JointState,'/joint_states',10)
        self.timer          = self.create_timer(1, self.lh_joint_pub_callback)


        self.resource_action = None

        if self.simulate_rviz:
            while self.resource_action is None:
                self.resource_action = self.check_tf_update_actions()
                time.sleep(1)

            self.resource_action_client = ActionClient(self, SendCmd, self.resource_action)
            while not self.resource_action_client.wait_for_server(timeout_sec=1.0):
                self.get_logger().info('等待 TfUpdate 服务器...')

        self.deck_list = []
        self.lh_devices = {}

        self.j_msg = JointState(
            name=[f'{self.lh_device_id}_{x}' for x in self.joint_config['joint_names']],
            position=[0.0 for _ in self.joint_config['joint_names']],
            velocity=[0.0 for _ in self.joint_config['joint_names']],
            effort=[0.0 for _ in self.joint_config['joint_names']]
        )

        # self.j_action       = ActionServer(
        #     self,
        #     SendCmd,
        #     "hl_joint_action",
        #     self.lh_joint_action_callback,
        #     result_timeout=5000
        # )

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
    
    def send_resource_action(self, resource_id_list:list[str], link_name:str):
        if self.simulate_rviz:
            goal_msg = SendCmd.Goal()
            str_dict = {}
            for resource in resource_id_list:
                str_dict[resource] = link_name

            goal_msg.command = json.dumps(str_dict)
        
            self.resource_action_client.send_goal(goal_msg)
        else:
            pass

    
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
        joint_positions = copy.deepcopy(self.j_msg.position)
        
        z_index = 0
        # 处理x轴关节
        for joint_name, config in x_joint.items():
            index = self.j_msg.name.index(f"{parent_id}_{joint_name}")
            joint_positions[index] = x * config["factor"] + config["offset"]
            
        # 处理y轴关节
        for joint_name, config in y_joint.items():
            index = self.j_msg.name.index(f"{parent_id}_{joint_name}")
            joint_positions[index] = y * config["factor"] + config["offset"]
            
        # 处理z轴关节
        for joint_name, config in z_joint.items():
            index = self.j_msg.name.index(f"{parent_id}_{joint_name}")
            joint_positions[index] = z * config["factor"] + config["offset"]
            z_index = index

        return joint_positions ,z_index


    def move_joints(self, resource_names, x, y, z, option, speed = 0.1 ,x_joint=None, y_joint=None, z_joint=None,channels=[0,1,2,3,4,5,6,7]):
        if isinstance(resource_names, list):
            resource_name_ = resource_names[0]
        else:
            resource_name_ = resource_names
        
        lh_device_id = self.lh_device_id


        # print('!'*20)
        # print(parent_id)
        # print('!'*20)
        if x_joint is None:
            xa,xb = next(iter(self.joint_config['x'].items()))
            x_joint_config = {xa:xb}
        elif x_joint in self.joint_config['x']:
            x_joint_config = self.joint_config['x'][x_joint]
        else:
            raise ValueError(f"x_joint {x_joint} not in joint_config['x']")
        if y_joint is None:
            ya,yb = next(iter(self.joint_config['y'].items()))
            y_joint_config = {ya:yb}
        elif y_joint in self.joint_config['y']:
            y_joint_config = self.joint_config['y'][y_joint]
        else:
            raise ValueError(f"y_joint {y_joint} not in joint_config['y']")
        if z_joint is None:
            za, zb = next(iter(self.joint_config['z'].items()))
            z_joint_config = {za :zb}
        elif z_joint in self.joint_config['z']:
            z_joint_config = self.joint_config['z'][z_joint]
        else:
            raise ValueError(f"z_joint {z_joint} not in joint_config['z']")

        joint_positions_target, z_index = self.inverse_kinematics(x,y,z,lh_device_id,x_joint_config,y_joint_config,z_joint_config)
        joint_positions_target_zero = copy.deepcopy(joint_positions_target)
        joint_positions_target_zero[z_index] = 0

        self.move_to(joint_positions_target_zero, speed)
        self.move_to(joint_positions_target, speed)
        time.sleep(1)
        if option == "pick":
            link_name =  self.joint_config['link_names'][z_index]
            link_name =  f'{lh_device_id}_{link_name}'
            self.resource_move(resource_name_, link_name, channels)
        elif option == "drop_trash":
            self.resource_move(resource_name_, "__trash", channels)
        elif option == "drop":
            self.resource_move(resource_name_, "world", channels)
        self.move_to(joint_positions_target_zero, speed)


    def move_to(self, joint_positions ,speed):
        loop_flag = 0

        while loop_flag < len(joint_positions):
            loop_flag = 0
            for i in range(len(joint_positions)):
                distance = joint_positions[i] - self.j_msg.position[i]
                if distance == 0:
                    loop_flag += 1
                    continue
                minus_flag = distance/abs(distance)
                if abs(distance) > speed/self.rate:
                    self.j_msg.position[i] += minus_flag * speed/self.rate
                else :
                    self.j_msg.position[i] = joint_positions[i]
                    loop_flag += 1
                    

            # 发布关节状态
            self.lh_joint_pub_callback()
            time.sleep(1/self.rate)

    def lh_joint_pub_callback(self):
        self.j_msg.header.stamp = self.get_clock().now().to_msg()
        self.j_pub.publish(self.j_msg)


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