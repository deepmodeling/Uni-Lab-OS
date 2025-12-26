from pathlib import Path
import time
import uuid
import rclpy,json
from rclpy.node import Node
from std_msgs.msg import String,Header
import numpy as np
from moveit_msgs.srv import GetPlanningScene, ApplyPlanningScene
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import QoSProfile, QoSDurabilityPolicy, QoSReliabilityPolicy, QoSHistoryPolicy
from moveit_msgs.msg import CollisionObject, AttachedCollisionObject, AllowedCollisionEntry, RobotState, PlanningScene
from shape_msgs.msg import Mesh, MeshTriangle, SolidPrimitive
from geometry_msgs.msg import Pose, PoseStamped, Point, Quaternion, TransformStamped
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import QoSProfile, QoSDurabilityPolicy, QoSReliabilityPolicy, QoSHistoryPolicy
from rclpy.task import Future
import copy
from typing import Tuple, Optional, Union, Any, List
from tf_transformations import quaternion_from_euler
from tf2_ros import TransformBroadcaster, Buffer, TransformListener 
from rclpy.action import ActionServer
from unilabos_msgs.action import SendCmd
from rclpy.action.server import ServerGoalHandle
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode,DeviceNodeResourceTracker
from unilabos.resources.graphio import initialize_resources
from unilabos.registry.registry import lab_registry

class ResourceMeshManager(BaseROS2DeviceNode):
    def __init__(self, resource_model: dict, resource_config: list,resource_tracker, device_id: str = "resource_mesh_manager", rate=50, **kwargs):
        """初始化资源网格管理器节点
        
        Args:
            resource_model (dict): 资源模型字典,包含资源的3D模型信息
            resource_config (dict): 资源配置字典,包含资源的配置信息
            device_id (str): 节点名称
        """
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

        self.resource_model         = resource_model
        self.resource_config_dict   = {item['uuid']: item for item in resource_config}
        self.move_group_ready       = False
        self.resource_tf_dict       = {}
        self.tf_broadcaster         = TransformBroadcaster(self)
        self.tf_buffer              = Buffer()
        self.tf_listener            = TransformListener(self.tf_buffer, self)
        self.rate                   = rate
        self.zero_count             = 0

        self.old_resource_pose      = {}
        self.__planning_scene       = PlanningScene()
        self.__old_planning_scene   = None
        self.__old_allowed_collision_matrix = None
        self.mesh_path = Path(__file__).parent.parent.parent.parent.absolute()
        self.msg_type = 'resource_status'
        self.resource_status_dict = {}
        
        callback_group = ReentrantCallbackGroup()
        self._get_planning_scene_service = self.create_client(
            srv_type=GetPlanningScene,
            srv_name="/get_planning_scene",
            qos_profile=QoSProfile(
                durability=QoSDurabilityPolicy.VOLATILE,
                reliability=QoSReliabilityPolicy.RELIABLE,
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=1,
            ),
            callback_group=callback_group,
        )
        
        # Create a service for applying the planning scene
        self._apply_planning_scene_service = self.create_client(
            srv_type=ApplyPlanningScene,
            srv_name="/apply_planning_scene",
            qos_profile=QoSProfile(
                durability=QoSDurabilityPolicy.VOLATILE,
                reliability=QoSReliabilityPolicy.RELIABLE,
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=1,
            ),
            callback_group=callback_group,
        )

        self.resource_pose_publisher = self.create_publisher(
            String, f"resource_pose", 10
        )
        self.__collision_object_publisher = self.create_publisher(
            CollisionObject, "/collision_object", 10
        )
        self.__planning_scene_publisher = self.create_publisher(
            PlanningScene, "/planning_scene", 10
        )
        self.__attached_collision_object_publisher = self.create_publisher(
            AttachedCollisionObject, "/attached_collision_object", 0
        )

        # 创建一个Action Server用于修改resource_tf_dict
        self._action_server = ActionServer(
            self,
            SendCmd,
            f"tf_update",
            self.tf_update,
            callback_group=callback_group
        )

        # 创建一个Action Server用于添加新的资源模型与resource_tf_dict
        self._add_resource_mesh_action_server = ActionServer(
            self,
            SendCmd,
            f"add_resource_mesh",
            self.add_resource_mesh_callback,
            callback_group=callback_group
        )

        self.resource_tf_dict = self.resource_mesh_setup(self.resource_config_dict)
        self.create_timer(1/self.rate, self.publish_resource_tf)
        self.create_timer(1/self.rate, self.check_resource_pose_changes)

    def check_move_group_ready(self):
        """检查move_group节点是否已初始化完成"""

        # 获取当前可用的节点列表
        if len(self.resource_tf_dict) == 0:
            return
        tf_ready = self.tf_buffer.can_transform("world", next(iter(self.resource_tf_dict.keys())), rclpy.time.Time(),rclpy.duration.Duration(seconds=2))
        
        # if tf_ready:
        if self._get_planning_scene_service.service_is_ready() and self._apply_planning_scene_service.service_is_ready() and tf_ready:
            self.move_group_ready = True
            self.publish_resource_tf()
            self.add_resource_collision_meshes(self.resource_tf_dict)

       
    def add_resource_mesh_callback(self, goal_handle : ServerGoalHandle):
        tf_update_msg = goal_handle.request
        try:    
            self.add_resource_mesh(tf_update_msg.command)
        except Exception as e:
            self.get_logger().error(f"添加资源失败: {e}")
            goal_handle.abort()
            return SendCmd.Result(success=False)
        goal_handle.succeed()
        return SendCmd.Result(success=True)
    
    def add_resource_mesh(self,resource_config_str:str):
        """刷新资源配置"""

        registry = lab_registry
        resource_config = json.loads(resource_config_str.replace("'",'"'))
        
        if resource_config['id'] in self.resource_config_dict:
            self.get_logger().info(f'资源 {resource_config["id"]} 已存在')
            return
        if resource_config['class'] in registry.resource_type_registry.keys():
            model_config = registry.resource_type_registry[resource_config['class']]['model']
            if model_config['type'] == 'resource':
                self.resource_model[resource_config['id']] = {
                    'mesh': f"{str(self.mesh_path)}/device_mesh/resources/{model_config['mesh']}",
                    'mesh_tf': model_config['mesh_tf']}
                if 'children_mesh' in model_config.keys():
                    self.resource_model[f"{resource_config['id']}_"] = {
                        'mesh': f"{str(self.mesh_path)}/device_mesh/resources/{model_config['children_mesh']}",
                        'mesh_tf': model_config['children_mesh_tf']
                    }
        resources = initialize_resources([resource_config])
        resource_dict = {item['id']: item for item in resources}
        self.resource_config_dict = {**self.resource_config_dict,**resource_dict}
        tf_dict = self.resource_mesh_setup(resource_dict)
        self.resource_tf_dict = {**self.resource_tf_dict,**tf_dict}
        self.publish_resource_tf()
        self.add_resource_collision_meshes(tf_dict)


    def resource_mesh_setup(self, resource_config_dict:dict):
        """move_group初始化完成后的设置"""
        self.get_logger().info('开始设置资源网格管理器')
        #遍历resource_config中的资源配置，判断panent是否在resource_model中，
        resource_tf_dict = {}
        for resource_uuid, resource_config in resource_config_dict.items():
            parent = None
            resource_id = resource_config['id']
            if resource_config['parent_uuid'] is not None and resource_config['parent_uuid'] != "":
                parent = resource_config_dict[resource_config['parent_uuid']]['id']

            parent_link = 'world'
            if parent in self.resource_model:
                parent_link = parent
            elif parent is None and resource_id in self.resource_model:
                pass
            elif parent is not None and resource_id in self.resource_model:
                # parent_link = f"{self.resource_config_dict[parent]['parent']}_{parent}_device_link".replace("None_","")
                parent_link = f"{parent}_device_link".replace("None_","")

            else:
                continue
            # 提取位置信息并转换单位
            position = {
                "x": float(resource_config['pose']['position']['x'])/1000,
                "y": float(resource_config['pose']['position']['y'])/1000,
                "z": float(resource_config['pose']['position']['z'])/1000
            }
            
            rotation_dict = {
                "x": 0,
                "y": 0,
                "z": 0
            }

            if 'rotation' in resource_config['pose']:
                rotation_dict = resource_config['pose']['rotation']   
                
            # 从欧拉角转换为四元数
            q = quaternion_from_euler(
                float(rotation_dict['x']), 
                float(rotation_dict['y']), 
                float(rotation_dict['z'])
            )

            rotation = {
                "x": q[0],
                "y": q[1],
                "z": q[2],
                "w": q[3]
            }

            # 更新资源TF字典
            resource_tf_dict[resource_id] = {
                "parent": parent_link,
                "position": position,
                "rotation": rotation
            }

        return resource_tf_dict


    def publish_resource_tf(self):
        """
        发布资源之间的TF关系
        
        遍历self.resource_tf_dict中的每个元素，根据key，parent，以及position和rotation，
        发布key和parent之间的tf关系
        """

        transforms = []
        
        # 遍历资源TF字典
        resource_tf_dict = copy.deepcopy(self.resource_tf_dict)
        for resource_id, tf_info in resource_tf_dict.items():
            parent = tf_info['parent']
            position = tf_info['position']
            rotation = tf_info['rotation']
            
            # 创建静态变换消息
            
            transform = TransformStamped()
            transform.header.stamp = self.get_clock().now().to_msg()
            transform.header.frame_id = parent
            transform.child_frame_id = resource_id
            
            # 设置位置
            transform.transform.translation.x = float(position['x'])
            transform.transform.translation.y = float(position['y'])
            transform.transform.translation.z = float(position['z'])
            
            # 设置旋转
            transform.transform.rotation.x = rotation['x']
            transform.transform.rotation.y = rotation['y']
            transform.transform.rotation.z = rotation['z']
            transform.transform.rotation.w = rotation['w']
            
            transforms.append(transform)
            
        # 一次性发布所有静态变换
        if transforms:
            self.tf_broadcaster.sendTransform(transforms)
            # self.check_resource_pose_changes()
            # self.get_logger().info(f'已发布 {len(transforms)} 个资源TF关系')


    def check_resource_pose_changes(self):
        """
        遍历资源TF字典，计算每个资源相对于world的变换，
        与旧的位姿比较，记录发生变化的资源，并更新旧位姿记录。
        
        Returns:
            dict: 包含发生位姿变化的资源ID及其新位姿
        """
        if not self.move_group_ready:
            self.check_move_group_ready()
            return
        changed_poses = {}
        resource_tf_dict = copy.deepcopy(self.resource_tf_dict)
        if self.msg_type == 'resource_pose':
            for resource_id in resource_tf_dict.keys():
                try:
                    # 获取从resource_id到world的转换

                    transform = self.tf_buffer.lookup_transform(
                        "world",
                        resource_id,
                        rclpy.time.Time(seconds=0),
                        # rclpy.duration.Duration(seconds=5)
                    )
                    
                    # 提取当前位姿信息
                    current_pose = {
                        "position": {
                            "x": transform.transform.translation.x,
                            "y": transform.transform.translation.y,
                            "z": transform.transform.translation.z
                        },
                        "rotation": {
                            "x": transform.transform.rotation.x,
                            "y": transform.transform.rotation.y,
                            "z": transform.transform.rotation.z,
                            "w": transform.transform.rotation.w
                        }
                    }
                    
                    # 检查是否存在旧位姿记录
                    if resource_id not in self.old_resource_pose:
                        # 如果没有旧记录，则认为是新资源，记录变化
                        changed_poses[resource_id] = current_pose
                        self.old_resource_pose[resource_id] = current_pose
                    else:
                        # 比较当前位姿与旧位姿
                        old_pose = self.old_resource_pose[resource_id]
                        if (not self._is_pose_equal(current_pose, old_pose)):
                            # 如果位姿发生变化，记录新位姿
                            changed_poses[resource_id] = current_pose
                            self.old_resource_pose[resource_id] = current_pose
                            
                except Exception as e:
                    self.get_logger().warning(f"获取资源 {resource_id} 的世界坐标变换失败: {e}")
                    
        elif self.msg_type == 'resource_status':
            for resource_id, resource_status in resource_tf_dict.items():
                if resource_id not in self.old_resource_pose:
                    # 如果没有旧记录，则认为是新资源，记录变化
                    changed_poses[resource_id] = resource_status['parent']
                    self.old_resource_pose[resource_id] = resource_status['parent']
                else:
                    # 比较当前位姿与旧位姿
                    old_pose = self.old_resource_pose[resource_id]
                    if resource_status['parent'] != old_pose:
                        # 如果位姿发生变化，记录新位姿
                        changed_poses[resource_id] = resource_status['parent']
                        self.old_resource_pose[resource_id] = resource_status['parent']
                    
                    
        if changed_poses != {}:
            self.zero_count = 0
            changed_poses_msg = String()
            changed_poses_msg.data = json.dumps(changed_poses)
            self.resource_pose_publisher.publish(changed_poses_msg)
        else:
            if self.zero_count > self.rate:
                self.zero_count = 0
                changed_poses_msg = String()
                changed_poses_msg.data = json.dumps(changed_poses)
                self.resource_pose_publisher.publish(changed_poses_msg)
            self.zero_count += 1
            

    def _is_pose_equal(self, pose1, pose2, tolerance=1e-7):
        """
        比较两个位姿是否相等（考虑浮点数精度）
        
        Args:
            pose1: 第一个位姿
            pose2: 第二个位姿
            tolerance: 浮点数比较的容差
            
        Returns:
            bool: 如果位姿相等返回True，否则返回False
        """
        # 比较位置
        pos1 = pose1["position"]
        pos2 = pose2["position"]
        if (abs(pos1["x"] - pos2["x"]) > tolerance or
            abs(pos1["y"] - pos2["y"]) > tolerance or
            abs(pos1["z"] - pos2["z"]) > tolerance):
            return False
            
        # 比较旋转
        rot1 = pose1["rotation"]
        rot2 = pose2["rotation"]
        if (abs(rot1["x"] - rot2["x"]) > tolerance or
            abs(rot1["y"] - rot2["y"]) > tolerance or
            abs(rot1["z"] - rot2["z"]) > tolerance or
            abs(rot1["w"] - rot2["w"]) > tolerance):
            return False
            
        return True

    def tf_update(self, goal_handle : ServerGoalHandle):
        tf_update_msg = goal_handle.request
        
        try:
            cmd_dict = json.loads(tf_update_msg.command.replace("'",'"'))
            self.__planning_scene = self._get_planning_scene_service.call(
                GetPlanningScene.Request()
                ).scene
            self.__planning_scene.is_diff = True
            planning_scene = PlanningScene()
            planning_scene.is_diff = True
            planning_scene.robot_state.is_diff = True
            # time_start = self.get_clock().now()
            time_start = rclpy.time.Time(seconds=0)
            count = 0

            for resource_id, target_parent in cmd_dict.items():
                parent_id = target_parent
                if target_parent == '__trash':
                    parent_id = 'world'
                # 获取从resource_id到target_parent的转换
                transform = self.tf_buffer.lookup_transform(
                    parent_id,
                    resource_id,
                    time_start,
                    timeout=rclpy.duration.Duration(seconds=10)
                )
                
                # 提取转换中的位置和旋转信息
                position = {
                    "x": transform.transform.translation.x,
                    "y": transform.transform.translation.y,
                    "z": transform.transform.translation.z
                }
                
                rotation = {
                    "x": transform.transform.rotation.x,
                    "y": transform.transform.rotation.y,
                    "z": transform.transform.rotation.z,
                    "w": transform.transform.rotation.w
                }
                
                self.resource_tf_dict[resource_id] = {
                    "parent": parent_id,
                    "position": position,
                    "rotation": rotation
                }
                
                
                # self.attach_collision_object(id=resource_id,link_name=target_parent)
                # time.sleep(0.02)
                operation_attach = CollisionObject.ADD
                operation_world = CollisionObject.REMOVE
                if target_parent == 'world':
                    operation_attach = CollisionObject.REMOVE
                    operation_world = CollisionObject.ADD
                elif target_parent == '__trash':
                    operation_attach = CollisionObject.REMOVE

                world_object = CollisionObject(
                    id=resource_id,
                    operation=operation_world
                )
                if target_parent != '__trash':
                    planning_scene.world.collision_objects.append(world_object)


                collision_object = AttachedCollisionObject(
                    object=CollisionObject(
                        id=resource_id,
                        operation=operation_attach   
                    )
                )
                if target_parent != 'world' and target_parent != '__trash':
                    collision_object.link_name = target_parent
                planning_scene.robot_state.attached_collision_objects.append(collision_object)

                count += 1

                if count > 30:
                    req = ApplyPlanningScene.Request()
                    req.scene = planning_scene
                    self.publish_resource_tf()
                    self._apply_planning_scene_service.call(req)
                    self.__planning_scene_publisher.publish(planning_scene)
                    count = 0

                    planning_scene = PlanningScene()
                    planning_scene.is_diff = True
                    planning_scene.robot_state.is_diff = True

            req = ApplyPlanningScene.Request()
            req.scene = planning_scene
            self.publish_resource_tf()
            self._apply_planning_scene_service.call(req)
            self.__planning_scene_publisher.publish(planning_scene)

            # self.__collision_object_publisher.publish(CollisionObject())

            
        except Exception as e:
            self.get_logger().error(f"更新资源TF字典失败: {e}")
            goal_handle.abort()
            return SendCmd.Result(success=False)
        goal_handle.succeed()
        return SendCmd.Result(success=True)



    def add_resource_collision_meshes(self,resource_tf_dict:dict):
        """
        遍历资源配置字典，为每个在resource_model中有对应模型的资源添加碰撞网格
        
        该方法检查每个资源ID是否在self.resource_model中有对应的3D模型文件路径，
        
        """
        self.get_logger().info('开始添加资源碰撞网格')

        self.__planning_scene = self._get_planning_scene_service.call(
            GetPlanningScene.Request()
        ).scene
        planning_scene = PlanningScene()
        planning_scene.is_diff = True
        count = 0
        for resource_id, tf_info in resource_tf_dict.items():
            
            if resource_id in self.resource_model:
                # 获取位置信息

                position = [
                    float(self.resource_model[resource_id]['mesh_tf'][0]),
                    float(self.resource_model[resource_id]['mesh_tf'][1]),
                    float(self.resource_model[resource_id]['mesh_tf'][2])
                ]
                
                # 获取旋转信息并转换为四元数

                q = quaternion_from_euler(
                    float(self.resource_model[resource_id]['mesh_tf'][3]), 
                    float(self.resource_model[resource_id]['mesh_tf'][4]), 
                    float(self.resource_model[resource_id]['mesh_tf'][5])
                )
                
                # 添加碰撞网格
                collision_object = self.get_collision_mesh(
                    filepath=self.resource_model[resource_id]['mesh'],
                    id=resource_id,
                    position=position,
                    quat_xyzw=q,
                    frame_id=resource_id
                )
                count += 1
                planning_scene.world.collision_objects.append(collision_object)
            elif f"{tf_info['parent']}_" in self.resource_model:
                # 获取资源的父级框架ID
                id_ = f"{tf_info['parent']}_"
                
                # 获取位置信息
                position = [
                    float(self.resource_model[id_]['mesh_tf'][0]),
                    float(self.resource_model[id_]['mesh_tf'][1]),
                    float(self.resource_model[id_]['mesh_tf'][2])
                ]
                
                # 获取旋转信息并转换为四元数

                q = quaternion_from_euler(
                    float(self.resource_model[id_]['mesh_tf'][3]), 
                    float(self.resource_model[id_]['mesh_tf'][4]), 
                    float(self.resource_model[id_]['mesh_tf'][5])
                )
                
                # 添加碰撞网格
                collision_object = self.get_collision_mesh(
                    filepath=self.resource_model[id_]['mesh'],
                    id=resource_id,
                    position=position,
                    quat_xyzw=q,
                    frame_id=resource_id
                )
                count += 1
                planning_scene.world.collision_objects.append(collision_object)

            if count > 30:
                req = ApplyPlanningScene.Request()
                req.scene = planning_scene
                self.publish_resource_tf()
                self._apply_planning_scene_service.call(req)
                self.__planning_scene_publisher.publish(planning_scene)
                count = 0
                
                planning_scene = PlanningScene()
                planning_scene.is_diff = True

        req = ApplyPlanningScene.Request()
        req.scene = planning_scene
        self.publish_resource_tf()
        self._apply_planning_scene_service.call(req)
        self.__planning_scene_publisher.publish(planning_scene)

        self.get_logger().info('资源碰撞网格添加完成')


    def add_collision_primitive(
        self,
        id: str,
        primitive_type: int,
        dimensions: Tuple[float, float, float],
        pose: Optional[Union[PoseStamped, Pose]] = None,
        position: Optional[Union[Point, Tuple[float, float, float]]] = None,
        quat_xyzw: Optional[
            Union[Quaternion, Tuple[float, float, float, float]]
        ] = None,
        frame_id: Optional[str] = None,
        operation: int = CollisionObject.ADD,
    ):
        """
        Add collision object with a primitive geometry specified by its dimensions.

        `primitive_type` can be one of the following:
            - `SolidPrimitive.BOX`
            - `SolidPrimitive.SPHERE`
            - `SolidPrimitive.CYLINDER`
            - `SolidPrimitive.CONE`
        """

        if (pose is None) and (position is None or quat_xyzw is None):
            raise ValueError(
                "Either `pose` or `position` and `quat_xyzw` must be specified!"
            )

        if isinstance(pose, PoseStamped):
            pose_stamped = pose
        elif isinstance(pose, Pose):
            pose_stamped = PoseStamped(
                header=Header(
                    stamp=self.get_clock().now().to_msg(),
                    frame_id=(
                        frame_id if frame_id is not None else self.__base_link_name
                    ),
                ),
                pose=pose,
            )
        else:
            if not isinstance(position, Point):
                position = Point(
                    x=float(position[0]), y=float(position[1]), z=float(position[2])
                )
            if not isinstance(quat_xyzw, Quaternion):
                quat_xyzw = Quaternion(
                    x=float(quat_xyzw[0]),
                    y=float(quat_xyzw[1]),
                    z=float(quat_xyzw[2]),
                    w=float(quat_xyzw[3]),
                )
            pose_stamped = PoseStamped(
                header=Header(
                    stamp=self.get_clock().now().to_msg(),
                    frame_id=(
                        frame_id if frame_id is not None else self.__base_link_name
                    ),
                ),
                pose=Pose(position=position, orientation=quat_xyzw),
            )

        msg = CollisionObject(
            header=pose_stamped.header,
            id=id,
            operation=operation,
            pose=pose_stamped.pose,
        )

        msg.primitives.append(
            SolidPrimitive(type=primitive_type, dimensions=dimensions)
        )

        self.__collision_object_publisher.publish(msg)

    def add_collision_box(
        self,
        id: str,
        size: Tuple[float, float, float],
        pose: Optional[Union[PoseStamped, Pose]] = None,
        position: Optional[Union[Point, Tuple[float, float, float]]] = None,
        quat_xyzw: Optional[
            Union[Quaternion, Tuple[float, float, float, float]]
        ] = None,
        frame_id: Optional[str] = None,
        operation: int = CollisionObject.ADD,
    ):
        """
        Add collision object with a box geometry specified by its size.
        """

        assert len(size) == 3, "Invalid size of the box!"

        self.add_collision_primitive(
            id=id,
            primitive_type=SolidPrimitive.BOX,
            dimensions=size,
            pose=pose,
            position=position,
            quat_xyzw=quat_xyzw,
            frame_id=frame_id,
            operation=operation,
        )

    def add_collision_sphere(
        self,
        id: str,
        radius: float,
        pose: Optional[Union[PoseStamped, Pose]] = None,
        position: Optional[Union[Point, Tuple[float, float, float]]] = None,
        quat_xyzw: Optional[
            Union[Quaternion, Tuple[float, float, float, float]]
        ] = None,
        frame_id: Optional[str] = None,
        operation: int = CollisionObject.ADD,
    ):
        """
        Add collision object with a sphere geometry specified by its radius.
        """

        if quat_xyzw is None:
            quat_xyzw = Quaternion(x=0.0, y=0.0, z=0.0, w=1.0)

        self.add_collision_primitive(
            id=id,
            primitive_type=SolidPrimitive.SPHERE,
            dimensions=[
                radius,
            ],
            pose=pose,
            position=position,
            quat_xyzw=quat_xyzw,
            frame_id=frame_id,
            operation=operation,
        )

    def add_collision_cylinder(
        self,
        id: str,
        height: float,
        radius: float,
        pose: Optional[Union[PoseStamped, Pose]] = None,
        position: Optional[Union[Point, Tuple[float, float, float]]] = None,
        quat_xyzw: Optional[
            Union[Quaternion, Tuple[float, float, float, float]]
        ] = None,
        frame_id: Optional[str] = None,
        operation: int = CollisionObject.ADD,
    ):
        """
        Add collision object with a cylinder geometry specified by its height and radius.
        """

        self.add_collision_primitive(
            id=id,
            primitive_type=SolidPrimitive.CYLINDER,
            dimensions=[height, radius],
            pose=pose,
            position=position,
            quat_xyzw=quat_xyzw,
            frame_id=frame_id,
            operation=operation,
        )

    def add_collision_cone(
        self,
        id: str,
        height: float,
        radius: float,
        pose: Optional[Union[PoseStamped, Pose]] = None,
        position: Optional[Union[Point, Tuple[float, float, float]]] = None,
        quat_xyzw: Optional[
            Union[Quaternion, Tuple[float, float, float, float]]
        ] = None,
        frame_id: Optional[str] = None,
        operation: int = CollisionObject.ADD,
    ):
        """
        Add collision object with a cone geometry specified by its height and radius.
        """

        self.add_collision_primitive(
            id=id,
            primitive_type=SolidPrimitive.CONE,
            dimensions=[height, radius],
            pose=pose,
            position=position,
            quat_xyzw=quat_xyzw,
            frame_id=frame_id,
            operation=operation,
        )

    def get_collision_mesh(
        self,
        filepath: Optional[str],
        id: str,
        pose: Optional[Union[PoseStamped, Pose]] = None,
        position: Optional[Union[Point, Tuple[float, float, float]]] = None,
        quat_xyzw: Optional[
            Union[Quaternion, Tuple[float, float, float, float]]
        ] = None,
        frame_id: Optional[str] = None,
        operation: int = CollisionObject.ADD,
        scale: Union[float, Tuple[float, float, float]] = 1.0,
        mesh: Optional[Any] = None,
    ):
        """
        Add collision object with a mesh geometry. Either `filepath` must be
        specified or `mesh` must be provided.
        Note: This function required 'trimesh' Python module to be installed.
        """

        # Load the mesh
        try:
            import trimesh
        except ImportError as err:
            raise ImportError(
                "Python module 'trimesh' not found! Please install it manually in order "
                "to add collision objects into the MoveIt 2 planning scene."
            ) from err

        # Check the parameters
        if (pose is None) and (position is None or quat_xyzw is None):
            raise ValueError(
                "Either `pose` or `position` and `quat_xyzw` must be specified!"
            )
        if (filepath is None and mesh is None) or (
            filepath is not None and mesh is not None
        ):
            raise ValueError("Exactly one of `filepath` or `mesh` must be specified!")
        if mesh is not None and not isinstance(mesh, trimesh.Trimesh):
            raise ValueError("`mesh` must be an instance of `trimesh.Trimesh`!")

        if isinstance(pose, PoseStamped):
            pose_stamped = pose
        elif isinstance(pose, Pose):
            pose_stamped = PoseStamped(
                header=Header(
                    stamp=self.get_clock().now().to_msg(),
                    frame_id=(
                        frame_id if frame_id is not None else self.__base_link_name
                    ),
                ),
                pose=pose,
            )
        else:
            if not isinstance(position, Point):
                position = Point(
                    x=float(position[0]), y=float(position[1]), z=float(position[2])
                )
            if not isinstance(quat_xyzw, Quaternion):
                quat_xyzw = Quaternion(
                    x=float(quat_xyzw[0]),
                    y=float(quat_xyzw[1]),
                    z=float(quat_xyzw[2]),
                    w=float(quat_xyzw[3]),
                )
            pose_stamped = PoseStamped(
                header=Header(
                    stamp=self.get_clock().now().to_msg(),
                    frame_id=(
                        frame_id if frame_id is not None else self.__base_link_name
                    ),
                ),
                pose=Pose(position=position, orientation=quat_xyzw),
            )

        msg = CollisionObject(
            header=pose_stamped.header,
            id=id,
            operation=operation,
            pose=pose_stamped.pose,
        )

        if filepath is not None:
            mesh = trimesh.load(filepath)

        # Scale the mesh
        if isinstance(scale, float):
            scale = (scale, scale, scale)
        if not (scale[0] == scale[1] == scale[2] == 1.0):
            # If the mesh was passed in as a parameter, make a copy of it to
            # avoid transforming the original.
            if filepath is not None:
                mesh = mesh.copy()
            # Transform the mesh
            transform = np.eye(4)
            np.fill_diagonal(transform, scale)
            mesh.apply_transform(transform)

        msg.meshes.append(
            Mesh(
                triangles=[MeshTriangle(vertex_indices=face) for face in mesh.faces],
                vertices=[
                    Point(x=vert[0], y=vert[1], z=vert[2]) for vert in mesh.vertices
                ],
            )
        )
        
        # self.__collision_object_publisher.publish(msg)
        return msg

    def add_collision_mesh(
        self,
        filepath: Optional[str],
        id: str,
        pose: Optional[Union[PoseStamped, Pose]] = None,
        position: Optional[Union[Point, Tuple[float, float, float]]] = None,
        quat_xyzw: Optional[
            Union[Quaternion, Tuple[float, float, float, float]]
        ] = None,
        frame_id: Optional[str] = None,
        operation: int = CollisionObject.ADD,
        scale: Union[float, Tuple[float, float, float]] = 1.0,
        mesh: Optional[Any] = None,
    ):
        """
        Add collision object with a mesh geometry. Either `filepath` must be
        specified or `mesh` must be provided.
        Note: This function required 'trimesh' Python module to be installed.
        """

        # Load the mesh
        try:
            import trimesh
        except ImportError as err:
            raise ImportError(
                "Python module 'trimesh' not found! Please install it manually in order "
                "to add collision objects into the MoveIt 2 planning scene."
            ) from err

        # Check the parameters
        if (pose is None) and (position is None or quat_xyzw is None):
            raise ValueError(
                "Either `pose` or `position` and `quat_xyzw` must be specified!"
            )
        if (filepath is None and mesh is None) or (
            filepath is not None and mesh is not None
        ):
            raise ValueError("Exactly one of `filepath` or `mesh` must be specified!")
        if mesh is not None and not isinstance(mesh, trimesh.Trimesh):
            raise ValueError("`mesh` must be an instance of `trimesh.Trimesh`!")

        if isinstance(pose, PoseStamped):
            pose_stamped = pose
        elif isinstance(pose, Pose):
            pose_stamped = PoseStamped(
                header=Header(
                    stamp=self.get_clock().now().to_msg(),
                    frame_id=(
                        frame_id if frame_id is not None else self.__base_link_name
                    ),
                ),
                pose=pose,
            )
        else:
            if not isinstance(position, Point):
                position = Point(
                    x=float(position[0]), y=float(position[1]), z=float(position[2])
                )
            if not isinstance(quat_xyzw, Quaternion):
                quat_xyzw = Quaternion(
                    x=float(quat_xyzw[0]),
                    y=float(quat_xyzw[1]),
                    z=float(quat_xyzw[2]),
                    w=float(quat_xyzw[3]),
                )
            pose_stamped = PoseStamped(
                header=Header(
                    stamp=self.get_clock().now().to_msg(),
                    frame_id=(
                        frame_id if frame_id is not None else self.__base_link_name
                    ),
                ),
                pose=Pose(position=position, orientation=quat_xyzw),
            )

        msg = CollisionObject(
            header=pose_stamped.header,
            id=id,
            operation=operation,
            pose=pose_stamped.pose,
        )

        if filepath is not None:
            mesh = trimesh.load(filepath)

        # Scale the mesh
        if isinstance(scale, float):
            scale = (scale, scale, scale)
        if not (scale[0] == scale[1] == scale[2] == 1.0):
            # If the mesh was passed in as a parameter, make a copy of it to
            # avoid transforming the original.
            if filepath is not None:
                mesh = mesh.copy()
            # Transform the mesh
            transform = np.eye(4)
            np.fill_diagonal(transform, scale)
            mesh.apply_transform(transform)

        msg.meshes.append(
            Mesh(
                triangles=[MeshTriangle(vertex_indices=face) for face in mesh.faces],
                vertices=[
                    Point(x=vert[0], y=vert[1], z=vert[2]) for vert in mesh.vertices
                ],
            )
        )
        
        self.__collision_object_publisher.publish(msg)

    def remove_collision_object(self, id: str):
        """
        Remove collision object specified by its `id`.
        """

        msg = CollisionObject()
        msg.id = id
        msg.operation = CollisionObject.REMOVE
        msg.header.stamp = self.get_clock().now().to_msg()
        self.__collision_object_publisher.publish(msg)

    def remove_collision_mesh(self, id: str):
        """
        Remove collision mesh specified by its `id`.
        Identical to `remove_collision_object()`.
        """

        self.remove_collision_object(id)

    def attach_collision_object(
        self,
        id: str,
        link_name: Optional[str] = None,
        touch_links: List[str] = [],
        weight: float = 0.0,
    ):
        """
        Attach collision object to the robot.
        """

        msg = AttachedCollisionObject(
            object=CollisionObject(id=id, operation=CollisionObject.ADD)
        )
        msg.link_name = link_name
        msg.touch_links = touch_links
        msg.weight = weight

        self.__attached_collision_object_publisher.publish(msg)

    def detach_collision_object(self, id: int):
        """
        Detach collision object from the robot.
        """

        msg = AttachedCollisionObject(
            object=CollisionObject(id=id, operation=CollisionObject.REMOVE)
        )
        self.__attached_collision_object_publisher.publish(msg)

    def detach_all_collision_objects(self):
        """
        Detach collision object from the robot.
        """

        msg = AttachedCollisionObject(
            object=CollisionObject(operation=CollisionObject.REMOVE)
        )
        self.__attached_collision_object_publisher.publish(msg)

    def move_collision(
        self,
        id: str,
        position: Union[Point, Tuple[float, float, float]],
        quat_xyzw: Union[Quaternion, Tuple[float, float, float, float]],
        frame_id: Optional[str] = None,
    ):
        """
        Move collision object specified by its `id`.
        """

        msg = CollisionObject()

        if not isinstance(position, Point):
            position = Point(
                x=float(position[0]), y=float(position[1]), z=float(position[2])
            )
        if not isinstance(quat_xyzw, Quaternion):
            quat_xyzw = Quaternion(
                x=float(quat_xyzw[0]),
                y=float(quat_xyzw[1]),
                z=float(quat_xyzw[2]),
                w=float(quat_xyzw[3]),
            )

        pose = Pose()
        pose.position = position
        pose.orientation = quat_xyzw
        msg.pose = pose
        msg.id = id
        msg.operation = CollisionObject.MOVE
        msg.header.frame_id = (
            frame_id if frame_id is not None else self.__base_link_name
        )
        msg.header.stamp = self.get_clock().now().to_msg()

        self.__collision_object_publisher.publish(msg)

    def update_planning_scene(self) -> bool:
        """
        Gets the current planning scene. Returns whether the service call was
        successful.
        """

        if not self._get_planning_scene_service.service_is_ready():
            self.get_logger().warn(
                f"Service '{self._get_planning_scene_service.srv_name}' is not yet available. Better luck next time!"
            )
            return False
        self.__planning_scene = self._get_planning_scene_service.call(
            GetPlanningScene.Request()
        ).scene
        return True

    def allow_collisions(self, id: str, allow: bool) -> Optional[Future]:
        """
        Takes in the ID of an element in the planning scene. Modifies the allowed
        collision matrix to (dis)allow collisions between that object and all other
        object.

        If `allow` is True, a plan will succeed even if the robot collides with that object.
        If `allow` is False, a plan will fail if the robot collides with that object.
        Returns whether it successfully updated the allowed collision matrix.

        Returns the future of the service call.
        """
        # Update the planning scene
        if not self.update_planning_scene():
            return None
        allowed_collision_matrix = self.__planning_scene.allowed_collision_matrix
        self.__old_allowed_collision_matrix = copy.deepcopy(allowed_collision_matrix)

        # Get the location in the allowed collision matrix of the object
        j = None
        if id not in allowed_collision_matrix.entry_names:
            allowed_collision_matrix.entry_names.append(id)
        else:
            j = allowed_collision_matrix.entry_names.index(id)
        # For all other objects, (dis)allow collisions with the object with `id`
        for i in range(len(allowed_collision_matrix.entry_values)):
            if j is None:
                allowed_collision_matrix.entry_values[i].enabled.append(allow)
            elif i != j:
                allowed_collision_matrix.entry_values[i].enabled[j] = allow
        # For the object with `id`, (dis)allow collisions with all other objects
        allowed_collision_entry = AllowedCollisionEntry(
            enabled=[allow for _ in range(len(allowed_collision_matrix.entry_names))]
        )
        if j is None:
            allowed_collision_matrix.entry_values.append(allowed_collision_entry)
        else:
            allowed_collision_matrix.entry_values[j] = allowed_collision_entry

        # Apply the new planning scene
        if not self._apply_planning_scene_service.service_is_ready():
            self.get_logger().warn(
                f"Service '{self._apply_planning_scene_service.srv_name}' is not yet available. Better luck next time!"
            )
            return None
        return self._apply_planning_scene_service.call_async(
            ApplyPlanningScene.Request(scene=self.__planning_scene)
        )

    def process_allow_collision_future(self, future: Future) -> bool:
        """
        Return whether the allow collision service call is done and has succeeded
        or not. If it failed, reset the allowed collision matrix to the old one.
        """
        if not future.done():
            return False

        # Get response
        resp = future.result()

        # If it failed, restore the old planning scene
        if not resp.success:
            self.__planning_scene.allowed_collision_matrix = (
                self.__old_allowed_collision_matrix
            )

        return resp.success

    def clear_all_collision_objects(self) -> Optional[Future]:
        """
        Removes all attached and un-attached collision objects from the planning scene.

        Returns a future for the ApplyPlanningScene service call.
        """
        # Update the planning scene
        if not self.update_planning_scene():
            return None
        self.__old_planning_scene = copy.deepcopy(self.__planning_scene)

        # Remove all collision objects from the planning scene
        self.__planning_scene.world.collision_objects = []
        self.__planning_scene.robot_state.attached_collision_objects = []

        # Apply the new planning scene
        if not self._apply_planning_scene_service.service_is_ready():
            self.get_logger().warn(
                f"Service '{self._apply_planning_scene_service.srv_name}' is not yet available. Better luck next time!"
            )
            return None
        return self._apply_planning_scene_service.call_async(
            ApplyPlanningScene.Request(scene=self.__planning_scene)
        )
