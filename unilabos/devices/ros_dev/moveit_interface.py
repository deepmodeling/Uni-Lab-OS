import json
import time
from copy import deepcopy
from pathlib import Path

from moveit_msgs.msg import JointConstraint, Constraints
from rclpy.action import ActionClient
from tf2_ros import Buffer, TransformListener
from unilabos_msgs.action import SendCmd

from unilabos.devices.ros_dev.moveit2 import MoveIt2
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class MoveitInterface:
    _ros_node: BaseROS2DeviceNode
    tf_buffer: Buffer
    tf_listener: TransformListener

    def __init__(self, moveit_type, joint_poses, rotation=None, device_config=None, **kwargs):
        self.device_config = device_config
        self.rotation = rotation
        self.data_config = json.load(
            open(
                f"{Path(__file__).parent.parent.parent.absolute()}/device_mesh/devices/{moveit_type}/config/move_group.json",
                encoding="utf-8",
            )
        )
        self.arm_move_flag = False
        self.move_option = ["pick", "place", "side_pick", "side_place"]
        self.joint_poses = joint_poses
        self.cartesian_flag = False
        self.mesh_group = ["reactor", "sample", "beaker"]
        self.moveit2 = {}
        self.resource_action = None
        self.resource_client = None
        self.resource_action_ok = False


    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self._ros_node)

        for move_group, config in self.data_config.items():

            base_link_name = f"{self._ros_node.device_id}_{config['base_link_name']}"
            end_effector_name = f"{self._ros_node.device_id}_{config['end_effector_name']}"
            joint_names = [f"{self._ros_node.device_id}_{name}" for name in config["joint_names"]]

            self.moveit2[f"{move_group}"] = MoveIt2(
                node=self._ros_node,
                joint_names=joint_names,
                base_link_name=base_link_name,
                end_effector_name=end_effector_name,
                group_name=f"{self._ros_node.device_id}_{move_group}",
                callback_group=self._ros_node.callback_group,
                use_move_group_action=True,
                ignore_new_calls_while_executing=True,
            )
            self.moveit2[f"{move_group}"].allowed_planning_time = 3.0

        self._ros_node.create_timer(1, self.wait_for_resource_action, callback_group=self._ros_node.callback_group)


    def wait_for_resource_action(self):
        if not self.resource_action_ok:

            while self.resource_action is None:
                self.resource_action = self.check_tf_update_actions()
                time.sleep(1)
            self.resource_client = ActionClient(self._ros_node, SendCmd, self.resource_action)
            self.resource_action_ok = True
            while not self.resource_client.wait_for_server(timeout_sec=5.0):
                self._ros_node.lab_logger().info("等待 TfUpdate 服务器...")

    def check_tf_update_actions(self):
        topics = self._ros_node.get_topic_names_and_types()
        for topic_item in topics:
            topic_name, topic_types = topic_item
            if "action_msgs/msg/GoalStatusArray" in topic_types:
                # 删除 /_action/status 部分

                base_name = topic_name.replace("/_action/status", "")
                # 检查最后一个部分是否为 tf_update
                parts = base_name.split("/")
                if parts and parts[-1] == "tf_update":
                    return base_name

        return None

    def set_position(self, command):
        """使用moveit 移动到指定点
        Args:
            command: A JSON-formatted string that includes quaternion, speed, position

                    position (list)     : 点的位置 [x,y,z]
                    quaternion (list)   : 点的姿态(四元数) [x,y,z,w]
                    move_group (string) : The move group moveit will plan
                    speed (float)       : The speed of the movement, speed > 0
                    retry (int)         : Retry times when moveit plan fails

        Returns:
            None
        """

        result = SendCmd.Result()
        cmd_str = command.replace("'", '"')
        cmd_dict = json.loads(cmd_str)
        self.moveit_task(**cmd_dict)
        return result

    def moveit_task(
        self, move_group, position, quaternion, speed=1, retry=10, cartesian=False, target_link=None, offsets=[0, 0, 0]
    ):

        speed_ = float(max(0.1, min(speed, 1)))

        self.moveit2[move_group].max_velocity = speed_
        self.moveit2[move_group].max_acceleration = speed_

        re_ = False

        pose_result = [x + y for x, y in zip(position, offsets)]
        # print(pose_result)

        while retry > -1 and not re_:

            self.moveit2[move_group].move_to_pose(
                target_link=target_link,
                position=pose_result,
                quat_xyzw=quaternion,
                cartesian=cartesian,
                # cartesian_fraction_threshold=0.0,
                cartesian_max_step=0.01,
                weight_position=1.0,
            )
            re_ = self.moveit2[move_group].wait_until_executed()
            retry += -1

        return re_

    def moveit_joint_task(self, move_group, joint_positions, joint_names=None, speed=1, retry=10):

        re_ = False

        joint_positions_ = [float(x) for x in joint_positions]

        speed_ = float(max(0.1, min(speed, 1)))

        self.moveit2[move_group].max_velocity = speed_
        self.moveit2[move_group].max_acceleration = speed_

        while retry > -1 and not re_:

            self.moveit2[move_group].move_to_configuration(joint_positions=joint_positions_, joint_names=joint_names)
            re_ = self.moveit2[move_group].wait_until_executed()

            retry += -1
            print(self.moveit2[move_group].compute_fk(joint_positions))
        return re_

    def resource_manager(self, resource, parent_link):
        goal_msg = SendCmd.Goal()
        str_dict = {}
        str_dict[resource] = parent_link

        goal_msg.command = json.dumps(str_dict)
        assert self.resource_client is not None
        self.resource_client.send_goal(goal_msg)

        return True

    def pick_and_place(self, command: str):
        """
        Using MoveIt to make the robotic arm pick or place materials to a target point.

        Args:
            command: A JSON-formatted string that includes option, target, speed, lift_height, mt_height

                    *option (string)    : Action type: pick/place/side_pick/side_place
                    *move_group (string): The move group moveit will plan
                    *status(string)     : Target pose
                    resource(string)    : The target resource
                    x_distance (float)  : The distance to the target in x direction(meters)
                    y_distance (float)  : The distance to the target in y direction(meters)
                    lift_height (float) : The height at which the material should be lifted(meters)
                    retry (float)       : Retry times when moveit plan fails
                    speed (float)       : The speed of the movement, speed > 0
        Returns:
            None
        """
        result = SendCmd.Result()

        try:
            cmd_str = str(command).replace("'", '"')
            cmd_dict = json.loads(cmd_str)

            if cmd_dict["option"] in self.move_option:
                option_index = self.move_option.index(cmd_dict["option"])
                place_flag = option_index % 2

                config = {}
                function_list = []

                status = cmd_dict["status"]
                joint_positions_ = self.joint_poses[cmd_dict["move_group"]][status]

                config.update({k: cmd_dict[k] for k in ["speed", "retry", "move_group"] if k in cmd_dict})

                # 夹取
                if not place_flag:
                    if "target" in cmd_dict.keys():
                        function_list.append(lambda: self.resource_manager(cmd_dict["resource"], cmd_dict["target"]))
                    else:
                        function_list.append(
                            lambda: self.resource_manager(
                                cmd_dict["resource"], self.moveit2[cmd_dict["move_group"]].end_effector_name
                            )
                        )
                else:
                    function_list.append(lambda: self.resource_manager(cmd_dict["resource"], "world"))

                constraints = []
                if "constraints" in cmd_dict.keys():

                    for i in range(len(cmd_dict["constraints"])):
                        v = float(cmd_dict["constraints"][i])
                        if v > 0:
                            constraints.append(
                                JointConstraint(
                                    joint_name=self.moveit2[cmd_dict["move_group"]].joint_names[i],
                                    position=joint_positions_[i],
                                    tolerance_above=v,
                                    tolerance_below=v,
                                    weight=1.0,
                                )
                            )

                if "lift_height" in cmd_dict.keys():
                    retval = None
                    retry = config.get("retry", 10)
                    while retval is None and retry > 0:
                        retval = self.moveit2[cmd_dict["move_group"]].compute_fk(joint_positions_)
                        time.sleep(0.1)
                        retry -= 1
                    if retval is None:
                        result.success = False
                        return result
                    pose = [retval.pose.position.x, retval.pose.position.y, retval.pose.position.z]
                    quaternion = [
                        retval.pose.orientation.x,
                        retval.pose.orientation.y,
                        retval.pose.orientation.z,
                        retval.pose.orientation.w,
                    ]

                    function_list = [
                        lambda: self.moveit_task(
                            position=[retval.pose.position.x, retval.pose.position.y, retval.pose.position.z],
                            quaternion=quaternion,
                            **config,
                            cartesian=self.cartesian_flag,
                        )
                    ] + function_list

                    pose[2] += float(cmd_dict["lift_height"])
                    function_list.append(
                        lambda: self.moveit_task(
                            position=pose, quaternion=quaternion, **config, cartesian=self.cartesian_flag
                        )
                    )
                    end_pose = pose

                    if "x_distance" in cmd_dict.keys() or "y_distance" in cmd_dict.keys():
                        if "x_distance" in cmd_dict.keys():
                            deep_pose = deepcopy(pose)
                            deep_pose[0] += float(cmd_dict["x_distance"])
                        elif "y_distance" in cmd_dict.keys():
                            deep_pose = deepcopy(pose)
                            deep_pose[1] += float(cmd_dict["y_distance"])

                        function_list = [
                            lambda: self.moveit_task(
                                position=pose, quaternion=quaternion, **config, cartesian=self.cartesian_flag
                            )
                        ] + function_list
                        function_list.append(
                            lambda: self.moveit_task(
                                position=deep_pose, quaternion=quaternion, **config, cartesian=self.cartesian_flag
                            )
                        )
                        end_pose = deep_pose

                    retval_ik = None
                    retry = config.get("retry", 10)
                    while retval_ik is None and retry > 0:
                        retval_ik = self.moveit2[cmd_dict["move_group"]].compute_ik(
                            position=end_pose, quat_xyzw=quaternion, constraints=Constraints(joint_constraints=constraints)
                        )
                        time.sleep(0.1)
                        retry -= 1
                    if retval_ik is None:
                        result.success = False
                        return result
                    position_ = [
                        retval_ik.position[retval_ik.name.index(i)]
                        for i in self.moveit2[cmd_dict["move_group"]].joint_names
                    ]
                    function_list = [
                        lambda: self.moveit_joint_task(
                            joint_positions=position_,
                            joint_names=self.moveit2[cmd_dict["move_group"]].joint_names,
                            **config,
                        )
                    ] + function_list
                else:
                    function_list = [
                        lambda: self.moveit_joint_task(**config, joint_positions=joint_positions_)
                    ] + function_list

                for i in range(len(function_list)):
                    if i == 0:
                        self.cartesian_flag = False
                    else:
                        self.cartesian_flag = True

                    re = function_list[i]()
                    if not re:
                        print(i, re)
                        result.success = False
                        return result
                result.success = True

        except Exception as e:
            print(e)
            self.cartesian_flag = False
            result.success = False

        return result

    def set_status(self, command: str):
        """
        Goto home position

            Args:
                command: A JSON-formatted string that includes speed
                        *status (string)    : The joint status moveit will plan
                        *move_group (string): The move group moveit will plan
                        separate (list)     : The joint index to be separated
                        lift_height (float) : The height at which the material should be lifted(meters)
                        x_distance (float)  : The distance to the target in x direction(meters)
                        y_distance (float)  : The distance to the target in y direction(meters)
                        speed (float)       : The speed of the movement, speed > 0
                        retry (float)       : Retry times when moveit plan fails

            Returns:
                None
        """

        result = SendCmd.Result()

        try:
            cmd_str = command.replace("'", '"')
            cmd_dict = json.loads(cmd_str)
            config = {}
            config["move_group"] = cmd_dict["move_group"]
            if "speed" in cmd_dict.keys():
                config["speed"] = cmd_dict["speed"]
            if "retry" in cmd_dict.keys():
                config["retry"] = cmd_dict["retry"]

            status = cmd_dict["status"]
            joint_positions_ = self.joint_poses[cmd_dict["move_group"]][status]
            re = self.moveit_joint_task(**config, joint_positions=joint_positions_)
            if not re:
                result.success = False
                return result
            result.success = True
        except Exception as e:
            print(e)
            result.success = False

        return result
