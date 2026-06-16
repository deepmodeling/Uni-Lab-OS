# coding:utf-8
"""
功能:
    机械臂坐标管理模块, 负责加载和管理YAML配置文件中的坐标数据
    提供统一的坐标访问接口, 支持位姿、关节角度、轨迹等多种类型
"""

import yaml
from ruamel.yaml import YAML
import logging
from pathlib import Path
from typing import Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class PositionData:
    """
    功能:
        单个位置点的数据封装类
    """

    def __init__(self, name: str, data: dict):
        """
        功能:
            初始化位置数据
        参数:
            name: 位置点名称
            data: 位置点配置字典
        """
        self.name = name
        self.pose = data.get('pose')  # 位姿[x,y,z,rx,ry,rz]
        self.joints = data.get('joints')  # 关节角度[j1,j2,j3,j4,j5,j6]
        self.speed = data.get('speed', 0.1)  # 速度百分比
        self.acceleration = data.get('acceleration', 0.1)  # 加速度百分比
        self.blend_radius = data.get('blend_radius', 0)  # 融合半径
        self.tool = data.get('tool', '')  # 工具坐标系
        self.wobj = data.get('wobj', '')  # 工件坐标系
        self.description = data.get('description', '')  # 描述信息
        self.descend_z = data.get('descend_z')  # 下探距离, 单位mm
        self.lift_z = data.get('lift_z')  # 提升距离, 单位mm

    def has_pose(self) -> bool:
        """
        功能:
            判断是否包含位姿数据
        返回:
            bool, True表示包含位姿数据
        """
        return self.pose is not None

    def has_joints(self) -> bool:
        """
        功能:
            判断是否包含关节角度数据
        返回:
            bool, True表示包含关节角度数据
        """
        return self.joints is not None

    def __repr__(self):
        return f"PositionData(name={self.name}, pose={self.pose}, joints={self.joints})"


class GripperData:
    """
    功能:
        夹爪配置数据封装类
    """

    def __init__(self, name: str, data: dict):
        """
        功能:
            初始化夹爪数据
        参数:
            name: 夹爪名称
            data: 夹爪配置字典
        """
        self.name = name
        self.slot = data.get('slot', 1)  # 快换料位编号(1-3, 对应bg1-bg3)
        self.description = data.get('description', '')

    def __repr__(self):
        return f"GripperData(name={self.name}, slot={self.slot})"


class MaterialData:
    """
    功能:
        物料类型配置数据封装类
    """

    def __init__(self, name: str, data: dict):
        """
        功能:
            初始化物料数据
        参数:
            name: 物料类型名称
            data: 物料配置字典
        """
        self.name = name
        self.gripper = data.get('gripper', '')  # 使用的夹爪名称
        self.descend_z_offset = data.get('descend_z_offset', 0)  # 下探距离偏移(mm)
        self.lift_z_offset = data.get('lift_z_offset', 0)  # 提升距离偏移(mm)
        self.description = data.get('description', '')

    def __repr__(self):
        return f"MaterialData(name={self.name}, gripper={self.gripper})"


class TrajectoryData:
    """
    功能:
        轨迹数据封装类, 包含多个路径点
    """

    def __init__(self, name: str, data: dict):
        """
        功能:
            初始化轨迹数据
        参数:
            name: 轨迹名称
            data: 轨迹配置字典
        """
        self.name = name
        self.description = data.get('description', '')
        self.waypoints = []

        # 解析路径点
        for wp_data in data.get('waypoints', []):
            wp_name = wp_data.get('name', f'waypoint_{len(self.waypoints)}')
            self.waypoints.append(PositionData(wp_name, wp_data))

    def get_waypoint(self, index: int) -> Optional[PositionData]:
        """
        功能:
            获取指定索引的路径点
        参数:
            index: 路径点索引
        返回:
            PositionData对象, 如果索引越界则返回None
        """
        if 0 <= index < len(self.waypoints):
            return self.waypoints[index]
        return None

    def get_waypoint_by_name(self, name: str) -> Optional[PositionData]:
        """
        功能:
            根据名称获取路径点
        参数:
            name: 路径点名称
        返回:
            PositionData对象, 如果未找到则返回None
        """
        for wp in self.waypoints:
            if wp.name == name:
                return wp
        return None

    def __len__(self):
        return len(self.waypoints)

    def __repr__(self):
        return f"TrajectoryData(name={self.name}, waypoints={len(self.waypoints)})"


class ToolData:
    """
    功能:
        工具坐标系数据封装类
    """

    def __init__(self, name: str, data: dict):
        """
        功能:
            初始化工具数据
        参数:
            name: 工具名称
            data: 工具配置字典
        """
        self.name = name
        self.tcp_offset = data.get('tcp_offset', [0, 0, 0, 0, 0, 0])
        self.payload = data.get('payload', [0, 0, 0, 0])
        self.inertia = data.get('inertia', [0, 0, 0, 0, 0, 0])
        self.description = data.get('description', '')

    def __repr__(self):
        return f"ToolData(name={self.name}, tcp_offset={self.tcp_offset})"


class WorkObjectData:
    """
    功能:
        工件坐标系数据封装类
    """

    def __init__(self, name: str, data: dict):
        """
        功能:
            初始化工件坐标系数据
        参数:
            name: 工件坐标系名称
            data: 工件坐标系配置字典
        """
        self.name = name
        self.offset = data.get('offset', [0, 0, 0, 0, 0, 0])
        self.description = data.get('description', '')

    def __repr__(self):
        return f"WorkObjectData(name={self.name}, offset={self.offset})"


class PositionManager:
    """
    功能:
        机械臂坐标管理器, 负责加载和管理所有坐标配置
    """

    def __init__(self, config_file: Optional[str] = None):
        """
        功能:
            初始化坐标管理器
        参数:
            config_file: YAML配置文件路径, 为None时使用默认路径
        """
        if config_file is None:
            # 默认配置文件路径
            current_dir = Path(__file__).parent.parent
            config_file = current_dir / 'config' / 'arm_positions.yaml'

        self.config_file = Path(config_file)
        self.positions = {}  # 所有位置点, 按类别组织
        self.trajectories = {}  # 所有轨迹
        self.tools = {}  # 所有工具坐标系
        self.workobjects = {}  # 所有工件坐标系
        self.global_params = {}  # 全局参数
        self.grippers = {}  # 夹爪配置
        self.gripper_storage = {}  # 夹爪存放位置
        self.materials = {}  # 物料类型配置

        self._load_config()

    def _load_config(self):
        """
        功能:
            从YAML文件加载配置
        """
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            if config is None:
                logger.warning(f"配置文件为空: {self.config_file}")
                return

            # 加载位置点(除了特殊类别)
            special_categories = ['trajectories', 'tools', 'workobjects', 'global_params',
                                  'grippers', 'gripper_storage', 'materials', 'station_calibration']
            for category, positions in config.items():
                if category in special_categories:
                    continue

                if not isinstance(positions, dict):
                    continue

                self.positions[category] = {}
                for pos_name, pos_data in positions.items():
                    self.positions[category][pos_name] = PositionData(pos_name, pos_data)

            # 加载轨迹
            if 'trajectories' in config:
                for traj_name, traj_data in config['trajectories'].items():
                    self.trajectories[traj_name] = TrajectoryData(traj_name, traj_data)

            # 加载工具坐标系
            if 'tools' in config:
                for tool_name, tool_data in config['tools'].items():
                    self.tools[tool_name] = ToolData(tool_name, tool_data)

            # 加载工件坐标系
            if 'workobjects' in config:
                for wobj_name, wobj_data in config['workobjects'].items():
                    self.workobjects[wobj_name] = WorkObjectData(wobj_name, wobj_data)

            # 加载夹爪配置
            if 'grippers' in config:
                for gripper_name, gripper_data in config['grippers'].items():
                    self.grippers[gripper_name] = GripperData(gripper_name, gripper_data)

            # 加载夹爪存放位置
            if 'gripper_storage' in config:
                for name, data in config['gripper_storage'].items():
                    self.gripper_storage[name] = PositionData(name, data)

            # 加载物料类型配置
            if 'materials' in config:
                for material_name, material_data in config['materials'].items():
                    self.materials[material_name] = MaterialData(material_name, material_data)

            # 加载全局参数
            self.global_params = config.get('global_params', {})

            logger.info(f"成功加载坐标配置: {self.config_file}")
            logger.debug(f"位置类别数: {len(self.positions)}, 轨迹数: {len(self.trajectories)}, "
                       f"工具数: {len(self.tools)}, 工件坐标系数: {len(self.workobjects)}, "
                       f"夹爪数: {len(self.grippers)}, 物料类型数: {len(self.materials)}")

        except FileNotFoundError:
            logger.error(f"配置文件不存在: {self.config_file}")
            raise
        except yaml.YAMLError as e:
            logger.error(f"YAML解析错误: {e}")
            raise
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise

    def reload(self):
        """
        功能:
            重新加载配置文件
        """
        logger.info("重新加载坐标配置")
        self.positions.clear()
        self.trajectories.clear()
        self.tools.clear()
        self.workobjects.clear()
        self.global_params.clear()
        self.grippers.clear()
        self.gripper_storage.clear()
        self.materials.clear()
        self._load_config()

    def get_category(self, category: str) -> Optional[Dict]:
        """
        功能:
            获取指定类别的所有位置点配置
        参数:
            category: 位置类别, 如'tray_position', 'safe_positions'等
        返回:
            Dict, 包含该类别下所有位置点的字典, 如果类别不存在则返回None
        """
        if category not in self.positions:
            logger.warning(f"位置类别不存在: {category}")
            return None

        # 返回原始配置数据字典
        result = {}
        for pos_name, pos_data in self.positions[category].items():
            result[pos_name] = {
                'pose': pos_data.pose,
                'joints': pos_data.joints,
                'speed': pos_data.speed,
                'acceleration': pos_data.acceleration,
                'descend_z': pos_data.descend_z,
                'lift_z': pos_data.lift_z,
                'description': pos_data.description,
                'blend_radius': pos_data.blend_radius,
                'tool': pos_data.tool,
                'wobj': pos_data.wobj
            }
        return result

    def get_position(self, category: str, name: str) -> Optional[PositionData]:
        """
        功能:
            获取指定类别和名称的位置点
        参数:
            category: 位置类别, 如'tray_pickup', 'material_place'等
            name: 位置名称, 如'tray_1', 'workbench_1'等
        返回:
            PositionData对象, 如果未找到则返回None
        """
        if category not in self.positions:
            logger.warning(f"位置类别不存在: {category}")
            return None

        if name not in self.positions[category]:
            logger.warning(f"位置点不存在: {category}.{name}")
            return None

        return self.positions[category][name]

    def get_trajectory(self, name: str) -> Optional[TrajectoryData]:
        """
        功能:
            获取指定名称的轨迹
        参数:
            name: 轨迹名称
        返回:
            TrajectoryData对象, 如果未找到则返回None
        """
        if name not in self.trajectories:
            logger.warning(f"轨迹不存在: {name}")
            return None

        return self.trajectories[name]

    def get_tool(self, name: str) -> Optional[ToolData]:
        """
        功能:
            获取指定名称的工具坐标系
        参数:
            name: 工具名称
        返回:
            ToolData对象, 如果未找到则返回None
        """
        if name not in self.tools:
            logger.warning(f"工具坐标系不存在: {name}")
            return None

        return self.tools[name]

    def get_workobject(self, name: str) -> Optional[WorkObjectData]:
        """
        功能:
            获取指定名称的工件坐标系
        参数:
            name: 工件坐标系名称
        返回:
            WorkObjectData对象, 如果未找到则返回None
        """
        if name not in self.workobjects:
            logger.warning(f"工件坐标系不存在: {name}")
            return None

        return self.workobjects[name]

    def list_categories(self) -> List[str]:
        """
        功能:
            列出所有位置类别
        返回:
            位置类别名称列表
        """
        return list(self.positions.keys())

    def list_positions(self, category: str) -> List[str]:
        """
        功能:
            列出指定类别下的所有位置点名称
        参数:
            category: 位置类别
        返回:
            位置点名称列表
        """
        if category not in self.positions:
            return []
        return list(self.positions[category].keys())

    def list_trajectories(self) -> List[str]:
        """
        功能:
            列出所有轨迹名称
        返回:
            轨迹名称列表
        """
        return list(self.trajectories.keys())

    def list_tools(self) -> List[str]:
        """
        功能:
            列出所有工具坐标系名称
        返回:
            工具坐标系名称列表
        """
        return list(self.tools.keys())

    def list_workobjects(self) -> List[str]:
        """
        功能:
            列出所有工件坐标系名称
        返回:
            工件坐标系名称列表
        """
        return list(self.workobjects.keys())

    def get_gripper(self, name: str) -> Optional[GripperData]:
        """
        功能:
            获取指定名称的夹爪配置
        参数:
            name: 夹爪名称
        返回:
            GripperData对象, 如果未找到则返回None
        """
        if name not in self.grippers:
            logger.warning(f"夹爪配置不存在: {name}")
            return None
        return self.grippers[name]

    def get_gripper_storage_position(self, gripper_name: str) -> Optional[PositionData]:
        """
        功能:
            获取夹爪存放位置
        参数:
            gripper_name: 夹爪名称
        返回:
            PositionData对象, 如果未找到则返回None
        """
        if gripper_name not in self.gripper_storage:
            logger.warning(f"夹爪存放位置不存在: {gripper_name}")
            return None
        return self.gripper_storage[gripper_name]

    def get_material(self, name: str) -> Optional[MaterialData]:
        """
        功能:
            获取指定名称的物料类型配置
        参数:
            name: 物料类型名称
        返回:
            MaterialData对象, 如果未找到则返回None
        """
        if name not in self.materials:
            logger.warning(f"物料类型配置不存在: {name}")
            return None
        return self.materials[name]

    def list_grippers(self) -> List[str]:
        """
        功能:
            列出所有夹爪名称
        返回:
            夹爪名称列表
        """
        return list(self.grippers.keys())

    def list_materials(self) -> List[str]:
        """
        功能:
            列出所有物料类型名称
        返回:
            物料类型名称列表
        """
        return list(self.materials.keys())

    def get_global_param(self, key: str, default=None):
        """
        功能:
            获取全局参数
        参数:
            key: 参数名称
            default: 默认值
        返回:
            参数值, 如果不存在则返回默认值
        """
        return self.global_params.get(key, default)

    def save_calibration_offset(self, station_name: str, offset: dict):
        """
        功能:
            保存工站校准偏移值到配置文件, 保留原有注释和格式
        参数:
            station_name: 工站名称, 如"shelf", "synthesis", "analysis"
            offset: 偏移值字典, 包含x, y, z, dx, dy, dz
        """
        try:
            # 使用ruamel.yaml保留注释和格式
            yaml_handler = YAML()
            yaml_handler.preserve_quotes = True
            yaml_handler.default_flow_style = False
            yaml_handler.width = 4096  # 设置足够大的行宽，避免自动换行

            # 读取当前配置文件
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = yaml_handler.load(f)

            if config is None:
                config = {}

            # 确保存在station_calibration节点
            if 'station_calibration' not in config:
                config['station_calibration'] = {}

            # 更新或创建该工站的校准偏移值
            if station_name not in config['station_calibration']:
                config['station_calibration'][station_name] = {}

            # 只更新偏移值字段, 保留其他可能存在的字段
            config['station_calibration'][station_name]['x'] = offset['x']
            config['station_calibration'][station_name]['y'] = offset['y']
            config['station_calibration'][station_name]['z'] = offset['z']
            config['station_calibration'][station_name]['dx'] = offset['dx']
            config['station_calibration'][station_name]['dy'] = offset['dy']
            config['station_calibration'][station_name]['dz'] = offset['dz']

            # 如果没有description字段, 则添加
            if 'description' not in config['station_calibration'][station_name]:
                config['station_calibration'][station_name]['description'] = f"{station_name}工站校准偏移值"

            # 写回配置文件, 保留注释和格式
            with open(self.config_file, 'w', encoding='utf-8') as f:
                yaml_handler.dump(config, f)

            logger.debug(f"成功保存工站 {station_name} 的校准偏移值到配置文件")

        except Exception as e:
            logger.error(f"保存校准偏移值失败: {e}")
            raise

    def get_calibration_offset(self, station_name: str) -> Optional[dict]:
        """
        功能:
            获取工站校准偏移值
        参数:
            station_name: 工站名称, 如"shelf", "synthesis", "analysis"
        返回:
            dict或None, 包含x, y, z, dx, dy, dz的偏移值字典, 如果不存在则返回None
        """
        try:
            # 读取配置文件
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            if config is None or 'station_calibration' not in config:
                logger.warning(f"配置文件中不存在station_calibration节点")
                return None

            if station_name not in config['station_calibration']:
                logger.warning(f"工站 {station_name} 的校准偏移值不存在")
                return None

            offset = config['station_calibration'][station_name]
            return {
                'x': offset.get('x', 0.0),
                'y': offset.get('y', 0.0),
                'z': offset.get('z', 0.0),
                'dx': offset.get('dx', 0.0),
                'dy': offset.get('dy', 0.0),
                'dz': offset.get('dz', 0.0)
            }

        except Exception as e:
            logger.error(f"读取校准偏移值失败: {e}")
            return None

    def save_tray_position(self, tray_name: str, pose: list, description: str = None):
        """
        功能:
            保存托盘位置的TCP位姿到配置文件, 使用多行带注释格式
        参数:
            tray_name: 托盘位置名称, 如"agv_tray_1", "shelf_tray_1-1"
            pose: TCP位姿列表[x, y, z, rx, ry, rz], 位置单位mm, 姿态单位rad
            description: 位置描述, 为None时保留原有描述
        """
        try:
            from ruamel.yaml.comments import CommentedSeq, CommentedMap
            from ruamel.yaml.tokens import CommentToken
            from ruamel.yaml.error import CommentMark

            # 使用ruamel.yaml保留注释和格式
            yaml_handler = YAML()
            yaml_handler.preserve_quotes = True
            yaml_handler.default_flow_style = False
            yaml_handler.width = 4096  # 设置足够大的行宽, 避免自动换行

            # 读取当前配置文件
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = yaml_handler.load(f)

            if config is None:
                config = {}

            # 确保存在tray_position节点
            if 'tray_position' not in config:
                config['tray_position'] = {}

            # 创建带注释的pose序列, 使用纯Python列表
            pose_list = [float(v) for v in pose]
            pose_seq = CommentedSeq(pose_list)
            pose_seq.fa.set_flow_style()
            pose_seq.fa.set_block_style()
            # 使用安全的方式添加行尾注释
            pose_comments = ['x, 单位mm', 'y, 单位mm', 'z, 单位mm', 'rx, 单位rad', 'ry, 单位rad', 'rz, 单位rad']
            for i, comment in enumerate(pose_comments):
                # 直接设置注释, 避免yaml_add_eol_comment的迭代问题
                pose_seq.ca.items[i] = [None, None, CommentToken(f'  # {comment}\n', CommentMark(0), None), None]

            # 检查托盘位置是否存在
            if tray_name not in config['tray_position']:
                # 新建托盘位置配置
                tray_config = CommentedMap()
                tray_config['pose'] = pose_seq
                tray_config['descend_z'] = -32
                tray_config['lift_z'] = 20
                tray_config['speed'] = 0.6
                tray_config['acceleration'] = 0.3
                tray_config['description'] = description if description else f"{tray_name}抓取/放置位置"
                # 添加行尾注释
                tray_config.ca.items['pose'] = [None, None, CommentToken('  # 抓取点位姿, 位置单位mm, 姿态单位rad\n', CommentMark(0), None), None]
                tray_config.ca.items['descend_z'] = [None, None, CommentToken('  # 过渡点到抓取点的下探距离, 沿工具坐标系Z轴, 负值表示下降, 单位mm\n', CommentMark(0), None), None]
                tray_config.ca.items['lift_z'] = [None, None, CommentToken('  # 提升距离, 沿工具坐标系Z轴, 正值表示上升, 单位mm\n', CommentMark(0), None), None]
                tray_config.ca.items['speed'] = [None, None, CommentToken('  # 运动速度百分比 (0-1)\n', CommentMark(0), None), None]
                tray_config.ca.items['acceleration'] = [None, None, CommentToken('  # 加速度百分比 (0-1)\n', CommentMark(0), None), None]
                config['tray_position'][tray_name] = tray_config
            else:
                # 更新现有托盘位置的pose
                tray_config = config['tray_position'][tray_name]
                tray_config['pose'] = pose_seq
                # 更新pose键的注释
                tray_config.ca.items['pose'] = [None, None, CommentToken('  # 抓取点位姿, 位置单位mm, 姿态单位rad\n', CommentMark(0), None), None]
                # 如果提供了新描述, 则更新
                if description is not None:
                    tray_config['description'] = description

            # 写回配置文件, 保留注释和格式
            with open(self.config_file, 'w', encoding='utf-8') as f:
                yaml_handler.dump(config, f)

            logger.info(f"成功保存托盘位置 {tray_name} 的TCP位姿到配置文件")

            # 重新加载配置以更新内存中的数据
            self.reload()

        except Exception as e:
            logger.error(f"保存托盘位置失败: {e}")
            raise

    def print_summary(self):
        """
        功能:
            打印配置摘要信息
        """
        print(f"\n{'='*60}")
        print(f"机械臂坐标配置摘要")
        print(f"{'='*60}")
        print(f"配置文件: {self.config_file}")
        print(f"\n位置类别:")
        for category in self.list_categories():
            positions = self.list_positions(category)
            print(f"  - {category}: {len(positions)}个位置点")
            for pos_name in positions:
                pos = self.get_position(category, pos_name)
                print(f"    * {pos_name}: {pos.description}")

        print(f"\n轨迹:")
        for traj_name in self.list_trajectories():
            traj = self.get_trajectory(traj_name)
            print(f"  - {traj_name}: {len(traj)}个路径点 - {traj.description}")

        print(f"\n工具坐标系:")
        for tool_name in self.list_tools():
            tool = self.get_tool(tool_name)
            print(f"  - {tool_name}: {tool.description}")

        print(f"\n工件坐标系:")
        for wobj_name in self.list_workobjects():
            wobj = self.get_workobject(wobj_name)
            print(f"  - {wobj_name}: {wobj.description}")

        print(f"\n全局参数:")
        for key, value in self.global_params.items():
            print(f"  - {key}: {value}")
        print(f"{'='*60}\n")
