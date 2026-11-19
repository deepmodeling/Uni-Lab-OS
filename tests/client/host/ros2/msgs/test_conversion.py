"""
转换测试

测试Python对象和ROS消息之间的转换功能。
"""

import unittest
from dataclasses import dataclass

from unilabos.client.host.ros2.msgs.message_converter import (
    convert_to_ros_msg,
    convert_from_ros_msg,
    convert_to_ros_msg_with_mapping,
    convert_from_ros_msg_with_mapping,
    resource_msg_to_dict,
    Point,
    Float64,
    String,
    Point3D,
    Resource,
)


# 定义一些测试数据类
@dataclass
class TestPoint:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class TestBasicConversion(unittest.TestCase):
    """测试基本类型转换"""

    def test_primitive_conversion(self):
        """测试原始类型转换"""
        # Float转换
        float_value = 3.14
        ros_float = convert_to_ros_msg(Float64, float_value)
        self.assertEqual(ros_float.data, float_value)

        # 反向转换
        py_float = convert_from_ros_msg(ros_float)
        self.assertEqual(py_float, float_value)

        # 字符串转换
        str_value = "hello"
        ros_str = convert_to_ros_msg(String, str_value)
        self.assertEqual(ros_str.data, str_value)

        # 反向转换
        py_str = convert_from_ros_msg(ros_str)
        self.assertEqual(py_str, str_value)

    def test_point_conversion(self):
        """测试点类型转换"""
        # 创建Point3D对象
        py_point = Point3D(x=1.0, y=2.0, z=3.0)

        # 转换为ROS Point
        ros_point = convert_to_ros_msg(Point, py_point)
        self.assertEqual(ros_point.x, py_point.x)
        self.assertEqual(ros_point.y, py_point.y)
        self.assertEqual(ros_point.z, py_point.z)

        # 反向转换
        py_point_back = convert_from_ros_msg(ros_point)
        self.assertEqual(py_point_back.x, py_point.x)
        self.assertEqual(py_point_back.y, py_point.y)
        self.assertEqual(py_point_back.z, py_point.z)

    def test_dataclass_conversion(self):
        """测试dataclass转换"""
        # 创建dataclass
        test_point = TestPoint(x=1.0, y=2.0, z=3.0)

        # 转换
        ros_point = convert_to_ros_msg(Point, test_point)
        self.assertEqual(ros_point.x, test_point.x)
        self.assertEqual(ros_point.y, test_point.y)
        self.assertEqual(ros_point.z, test_point.z)


class TestMappingConversion(unittest.TestCase):
    """测试映射转换功能"""

    def test_mapping_conversion(self):
        """测试带映射的转换"""
        # 创建测试数据
        test_data = {
            "position": {"x": 1.0, "y": 2.0, "z": 3.0},
            "name": "test_resource",
            "id": "123",
            "type": "test_type",
        }

        # 定义映射
        mapping = {
            "id": "id",
            "name": "name",
            "type": "type",
            "pose.position": "position",
        }

        # 转换为ROS资源
        ros_resource = convert_to_ros_msg_with_mapping(Resource, test_data, mapping)
        self.assertEqual(ros_resource.id, "123")
        self.assertEqual(ros_resource.name, "test_resource")
        self.assertEqual(ros_resource.type, "test_type")
        self.assertEqual(ros_resource.pose.position.x, 1.0)
        self.assertEqual(ros_resource.pose.position.y, 2.0)
        self.assertEqual(ros_resource.pose.position.z, 3.0)

        # 反向转换
        reverse_mapping = {
            "id": "id",
            "name": "name",
            "type": "type",
            "pose.position": "position",
        }

        py_data = convert_from_ros_msg_with_mapping(ros_resource, reverse_mapping)
        self.assertEqual(py_data["id"], "123")
        self.assertEqual(py_data["name"], "test_resource")
        self.assertEqual(py_data["type"], "test_type")
        self.assertEqual(py_data["position"].x, 1.0)
        self.assertEqual(py_data["position"].y, 2.0)
        self.assertEqual(py_data["position"].z, 3.0)


class TestResourceGoalField(unittest.TestCase):
    """goal 里的单个 Resource 字段（如 ``Stir.vessel``）：引用整体进 data，未指定的容器发空消息。"""

    def test_specified_vessel_keeps_its_reference(self):
        from unilabos_msgs.action import Stir

        reference = {"id": "beaker", "uuid": "6c8e2f7a-9a51-4c43-8f0e-2d4b1a7f5e10"}
        goal = convert_to_ros_msg(Stir.Goal(), {"vessel": reference, "stir_time": 2.0})

        self.assertEqual(resource_msg_to_dict(goal.vessel), reference)
        self.assertEqual(goal.vessel.id, "beaker")
        self.assertEqual(goal.stir_time, 2.0)

    def test_unset_vessel_is_an_empty_resource(self):
        from unilabos_msgs.action import Stir

        for unset in (None, "", {}):
            goal = convert_to_ros_msg(Stir.Goal(), {"vessel": unset, "stir_time": 2.0})
            self.assertEqual(goal.vessel, Resource(), unset)
            self.assertEqual(resource_msg_to_dict(goal.vessel), {})
        self.assertEqual(convert_to_ros_msg(Stir.Goal(), {"stir_time": 2.0}).vessel, Resource())


if __name__ == "__main__":
    unittest.main()
