import json
import os
from pathlib import Path
import re

import yaml
from launch import LaunchService
from launch import LaunchDescription
from launch_ros.actions import Node as nd
import xacro
from lxml import etree
from launch_param_builder import load_yaml
from launch_ros.parameter_descriptions import ParameterFile
from unilabos.registry.registry import lab_registry
from ament_index_python.packages import get_package_share_directory


def get_pattern_matches(folder, pattern):
    """Given all the files in the folder, find those that match the pattern.

    If there are groups defined, the groups are returned. Otherwise the path to the matches are returned.
    """
    matches = []
    if not folder.exists():
        return matches
    for child in folder.iterdir():
        if not child.is_file():
            continue
        m = pattern.search(child.name)
        if m:
            groups = m.groups()
            if groups:
                matches.append(groups[0])
            else:
                matches.append(child)
    return matches

class ResourceVisualization:
    def __init__(self, device: dict, resource: dict, enable_rviz: bool = True):
        """初始化资源可视化类
        
        该类用于将设备和资源的3D模型可视化展示。通过解析设备和资源的配置信息,
        从注册表中获取对应的3D模型文件,并使用ROS2和RViz进行可视化。
        
        Args:
            device (dict): 设备配置字典,包含设备的类型、位置等信息
            resource (dict): 资源配置字典,包含资源的类型、位置等信息 
            registry (dict): 注册表字典,包含设备和资源类型的注册信息
            enable_rviz (bool, optional): 是否启用RViz可视化. Defaults to True.
        """
        self.launch_service = LaunchService()
        self.launch_description = LaunchDescription()
        self.resource_dict = resource
        self.resource_model = {}
        self.resource_type = ['deck', 'plate', 'container', 'tip_rack']
        self.mesh_path = Path(__file__).parent.absolute()
        self.enable_rviz = enable_rviz
        registry = lab_registry

        self.srdf_str = '''<?xml version="1.0" ?>
        <robot xmlns:xacro="http://ros.org/wiki/xacro" name="full_dev">

        </robot>
        '''
        self.robot_state_str= '''<?xml version="1.0" ?>
        <robot xmlns:xacro="http://ros.org/wiki/xacro" name="full_dev">
        <link name="world"/>
        </robot>
        '''
        self.root = etree.fromstring(self.robot_state_str)
        self.root_srdf = etree.fromstring(self.srdf_str)
                
        xacro_uri = self.root.nsmap["xacro"]

        self.moveit_nodes = {}
        self.moveit_nodes_kinematics = {}
        self.moveit_controllers_yaml = {
            "moveit_controller_manager": "moveit_simple_controller_manager/MoveItSimpleControllerManager",
            "moveit_simple_controller_manager": {
                "controller_names": []
            }
        }
        self.ros2_controllers_yaml = {
            "controller_manager": {
                "ros__parameters": {
                    "update_rate": 100,
                    "joint_state_broadcaster": {
                        "type": "joint_state_broadcaster/JointStateBroadcaster",
                    }
                }
            }
        }

        # 遍历设备节点
        for node in device.values():
            if (node['type'] in self.resource_type and node['class'] != '') or (node['type'] == 'device' and node['class'] != ''):
                model_config = {}
                if node['type'] in self.resource_type:
                    resource_class = node['class']
                    if resource_class not in registry.resource_type_registry.keys():
                        raise ValueError(f"{node['id']}资源类型 {resource_class} 未在注册表中注册")
                    elif "model" in registry.resource_type_registry[resource_class].keys():
                        model_config = registry.resource_type_registry[resource_class]['model']
                elif node['type'] == 'device' and node['class'] != '':
                    device_class = node['class']
                    if device_class not in registry.device_type_registry.keys():
                        raise ValueError(f"{node['id']}设备类型 {device_class} 未在注册表中注册")
                    elif "model" in registry.device_type_registry[device_class].keys():
                        model_config = registry.device_type_registry[device_class]['model']
                if model_config:
                    if model_config['type'] == 'resource':
                        self.resource_model[node['id']] = {
                            'mesh': f"{str(self.mesh_path)}/resources/{model_config['mesh']}",
                            'mesh_tf': model_config['mesh_tf']}
                        if 'children_mesh' in model_config:
                            if model_config['children_mesh'] is not None:
                                self.resource_model[f"{node['id']}_"] = {
                                    'mesh': f"{str(self.mesh_path)}/resources/{model_config['children_mesh']}",
                                    'mesh_tf': model_config['children_mesh_tf']
                                }
                    elif model_config['type'] == 'device':

                        new_include = etree.SubElement(self.root, f"{{{xacro_uri}}}include")
                        new_include.set("filename", f"{str(self.mesh_path)}/devices/{model_config['mesh']}/macro_device.xacro")
                        new_dev = etree.SubElement(self.root, f"{{{xacro_uri}}}{model_config['mesh']}")
                        new_dev.set("parent_link", "world")
                        new_dev.set("mesh_path", str(self.mesh_path))
                        new_dev.set("device_name", node["id"]+"_")
                        # if node["parent"] is not None:
                        #     new_dev.set("station_name", node["parent"]+'_')

                        new_dev.set("x",str(float(node["position"]["position"]["x"])/1000))
                        new_dev.set("y",str(float(node["position"]["position"]["y"])/1000))
                        new_dev.set("z",str(float(node["position"]["position"]["z"])/1000))
                        if "rotation" in node["config"]:
                            new_dev.set("rx",str(float(node["config"]["rotation"]["x"])))
                            new_dev.set("ry",str(float(node["config"]["rotation"]["y"])))
                            new_dev.set("r",str(float(node["config"]["rotation"]["z"])))
                        if "device_config" in node["config"]:
                            for key, value in node["config"]["device_config"].items():
                                new_dev.set(key, str(value))

                        # 添加ros2_controller
                        if node['class'].find('moveit.')!= -1:
                            new_include_controller = etree.SubElement(self.root, f"{{{xacro_uri}}}include")
                            new_include_controller.set("filename", f"{str(self.mesh_path)}/devices/{model_config['mesh']}/config/macro.ros2_control.xacro")
                            new_controller = etree.SubElement(self.root, f"{{{xacro_uri}}}{model_config['mesh']}_ros2_control")
                            new_controller.set("device_name", node["id"]+"_")
                            new_controller.set("mesh_path", str(self.mesh_path))

                            # 添加moveit的srdf
                            new_include_srdf = etree.SubElement(self.root_srdf, f"{{{xacro_uri}}}include")
                            new_include_srdf.set("filename", f"{str(self.mesh_path)}/devices/{model_config['mesh']}/config/macro.srdf.xacro")
                            new_srdf = etree.SubElement(self.root_srdf, f"{{{xacro_uri}}}{model_config['mesh']}_srdf")
                            new_srdf.set("device_name", node["id"]+"_")
                            self.moveit_nodes[node["id"]] = model_config['mesh']
                    else:
                        print("错误的注册表类型！")
        re = etree.tostring(self.root, encoding="unicode")
        doc = xacro.parse(re)
        xacro.process_doc(doc)
        self.urdf_str = doc.toxml()


        re_srdf = etree.tostring(self.root_srdf, encoding="unicode")
        doc_srdf = xacro.parse(re_srdf)
        xacro.process_doc(doc_srdf)
        self.urdf_str_srdf = doc_srdf.toxml()


        if self.moveit_nodes:
            self.moveit_init()

    def moveit_init(self):

        for name, config in self.moveit_nodes.items():
            controller_dict = yaml.safe_load(open(f"{str(self.mesh_path)}/devices/{config}/config/ros2_controllers.yaml", "r"))
            moveit_dict = yaml.safe_load(open(f"{str(self.mesh_path)}/devices/{config}/config/moveit_controllers.yaml", "r"))
            kinematics_dict = yaml.safe_load(open(f"{str(self.mesh_path)}/devices/{config}/config/kinematics.yaml", "r"))
            
            for key_kinematics, value_kinematics in kinematics_dict.items():
                self.moveit_nodes_kinematics[f'{name}_{key_kinematics}'] = value_kinematics
            
            for key, value in controller_dict['controller_manager']['ros__parameters'].items():
                if key == 'update_rate' or key == 'joint_state_broadcaster':
                    continue
                self.ros2_controllers_yaml['controller_manager']['ros__parameters'][f"{name}_{key}"] = value
                controller_dict[key]['ros__parameters']['joints'] = [f"{name}_{joint}" for joint in controller_dict[key]['ros__parameters']['joints']]
                self.ros2_controllers_yaml[f"{name}_{key}"] = controller_dict[key]

            for controller_name in moveit_dict['moveit_simple_controller_manager']['controller_names']:
                self.moveit_controllers_yaml['moveit_simple_controller_manager']['controller_names'].append(f"{name}_{controller_name}")
                moveit_dict['moveit_simple_controller_manager'][controller_name]['joints'] = [f"{name}_{joint}" for joint in moveit_dict['moveit_simple_controller_manager'][controller_name]['joints']]
                self.moveit_controllers_yaml['moveit_simple_controller_manager'][f"{name}_{controller_name}"] = moveit_dict['moveit_simple_controller_manager'][controller_name]


    def create_launch_description(self) -> LaunchDescription:
        """
        创建launch描述，包含robot_state_publisher和move_group节点

        Args:
            urdf_str: URDF文本

        Returns:
            LaunchDescription: launch描述对象
        """
        # 检查ROS 2环境变量
        if "AMENT_PREFIX_PATH" not in os.environ:
            raise OSError(
                "ROS 2环境未正确设置。需要设置 AMENT_PREFIX_PATH 环境变量。\n"
                "请确保：\n"
                "1. 已安装ROS 2 (推荐使用 ros-humble-desktop-full)\n"
                "2. 已激活Conda环境: conda activate unilab\n"
                "3. 或手动source ROS 2 setup文件: source /opt/ros/humble/setup.bash\n"
                "4. 或者使用 --backend simple 参数跳过ROS依赖"
            )

        try:
            moveit_configs_utils_path = Path(get_package_share_directory("moveit_configs_utils"))
        except Exception as e:
            raise OSError(
                f"无法找到moveit_configs_utils包。请确保ROS 2和MoveIt 2已正确安装。\n"
                f"原始错误: {e}"
            )
        default_folder = moveit_configs_utils_path / "default_configs"
        planning_pattern = re.compile("^(.*)_planning.yaml$")
        pipelines = []

        for pipeline in get_pattern_matches(default_folder, planning_pattern):
            if pipeline not in pipelines:
                pipelines.append(pipeline)

        if "ompl" in pipelines:
            default_planning_pipeline = "ompl"
        else:
            default_planning_pipeline = pipelines[0]

        planning_pipelines = {
            "planning_pipelines": pipelines,
            "default_planning_pipeline": default_planning_pipeline,
        }

        for pipeline in pipelines:
            planning_pipelines[pipeline] = load_yaml(
                default_folder /  f"{pipeline}_planning.yaml"
            )

        if "ompl" in planning_pipelines:
            ompl_config = planning_pipelines["ompl"]
            if "planner_configs" not in ompl_config:
                ompl_config.update(load_yaml(default_folder / "ompl_defaults.yaml"))

        yaml.safe_dump(self.ros2_controllers_yaml, open(f"{str(self.mesh_path)}/ros2_controllers.yaml", "w"))

        robot_description_planning = {
            "default_velocity_scaling_factor": 0.1,
            "default_acceleration_scaling_factor": 0.1,
            "cartesian_limits": {
            "max_trans_vel": 1.0,
            "max_trans_acc": 2.25,
            "max_trans_dec": -5.0,
            "max_rot_vel": 1.57
            }
        }
        # 解析URDF文件
        robot_description = self.urdf_str
        urdf_str_srdf = self.urdf_str_srdf

        kinematics_dict = self.moveit_nodes_kinematics


        if self.moveit_nodes:

            controllers = []
            ros2_controllers = ParameterFile(f"{str(self.mesh_path)}/ros2_controllers.yaml", allow_substs=True)

            controllers.append(
                nd(
                    package="controller_manager",
                    executable="ros2_control_node",
                    output='screen',
                    parameters=[
                        {"robot_description": robot_description},
                        ros2_controllers,
                    ],
                    env=dict(os.environ)
                )
            )
            for controller in self.moveit_controllers_yaml['moveit_simple_controller_manager']['controller_names']:
                controllers.append(
                    nd(
                        package="controller_manager",
                        executable="spawner",
                        arguments=[f"{controller}", "--controller-manager", f"controller_manager"],
                        output="screen",
                        env=dict(os.environ)
                    )
                )
            controllers.append(
                nd(
                        package="controller_manager",
                        executable="spawner",
                        arguments=["joint_state_broadcaster", "--controller-manager", f"controller_manager"],
                        output="screen",
                        env=dict(os.environ)
                )
            )
            for i in controllers:
                self.launch_description.add_action(i)
        else:
            ros2_controllers = None

        # 创建robot_state_publisher节点
        robot_state_publisher = nd(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            output='screen',
            parameters=[{
                'robot_description': robot_description,
                'use_sim_time': False
            },
            # kinematics_dict
            ],
            env=dict(os.environ)
        )


        # 创建move_group节点
        moveit_params =[{
            'allow_trajectory_execution': True,
            'robot_description': robot_description,
            'robot_description_semantic': urdf_str_srdf,
            'robot_description_kinematics': kinematics_dict,
            'capabilities': '',
            'disable_capabilities': '',
            'monitor_dynamics': False,
            'publish_monitored_planning_scene': True,
            'publish_robot_description_semantic': True,
            'publish_planning_scene': True,
            'publish_geometry_updates': True,
            'publish_state_updates': True,
            'publish_transforms_updates': True,
            # 'robot_description_planning': robot_description_planning,
            },
            robot_description_planning,
            planning_pipelines,
            ]
        if self.moveit_controllers_yaml['moveit_simple_controller_manager']['controller_names']:
            moveit_params.append(self.moveit_controllers_yaml)

        move_group = nd(
            package='moveit_ros_move_group',
            executable='move_group',
            output='screen',
            parameters=moveit_params,
            env=dict(os.environ)
        )


        # 将节点添加到launch描述中
        self.launch_description.add_action(robot_state_publisher)
        # self.launch_description.add_action(joint_state_publisher_node)
        self.launch_description.add_action(move_group)

        # 如果启用RViz,添加RViz节点
        if self.enable_rviz:
            rviz_node = nd(
                package='rviz2',
                executable='rviz2',
                name='rviz2',
                arguments=['-d', f"{str(self.mesh_path)}/view_robot.rviz"],
                output='screen',
                parameters=[
                    {'robot_description_kinematics': kinematics_dict,
                     },
                    robot_description_planning,
                    planning_pipelines,

                ],
                env=dict(os.environ)
            )
            self.launch_description.add_action(rviz_node)

        return self.launch_description

    def start(self) -> None:
        """
        启动可视化服务

        Args:
            urdf_str: URDF文件路径
        """
        launch_description = self.create_launch_description()
        # print('--------------------------------')
        # print(self.moveit_controllers_yaml)
        # print('--------------------------------')
        # print(self.urdf_str)
        # print('--------------------------------')
        self.launch_service.include_launch_description(launch_description)
        self.launch_service.run()