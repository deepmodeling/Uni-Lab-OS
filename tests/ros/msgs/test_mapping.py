"""
映射测试

测试消息类型映射和字段映射功能。
"""

import unittest
from dataclasses import dataclass

from unilabos.ros.msgs.message_converter import (
    _msg_mapping,
    _action_mapping,
    _msg_converter,
    _msg_converter_back,
    compare_model_fields,
    Point,
    Point3D,
    Float64,
    String,
    set_msg_data,
)


@dataclass
class TestMappingModel:
    """用于测试映射的数据类"""

    id: str
    name: str
    value: float


@dataclass
class TestPointModel:
    """用于测试字段比较的点模型"""

    x: float
    y: float
    z: float


class TestTypeMapping(unittest.TestCase):
    """测试类型映射"""

    def test_msg_mapping(self):
        """测试消息类型映射"""
        self.assertIn(float, _msg_mapping)
        self.assertEqual(_msg_mapping[float], Float64)

        self.assertIn(str, _msg_mapping)
        self.assertEqual(_msg_mapping[str], String)

        self.assertIn(Point3D, _msg_mapping)
        self.assertEqual(_msg_mapping[Point3D], Point)

    def test_action_mapping(self):
        """测试动作类型映射"""
        self.assertIn(float, _action_mapping)
        self.assertIn("type", _action_mapping[float])
        self.assertIn("goal", _action_mapping[float])
        self.assertIn("feedback", _action_mapping[float])
        self.assertIn("result", _action_mapping[float])

    def test_converter_mapping(self):
        """测试转换器映射"""
        # 测试Python到ROS映射
        self.assertIn(float, _msg_converter)
        self.assertIn(Float64, _msg_converter)
        self.assertIn(String, _msg_converter)
        self.assertIn(Point, _msg_converter)

        # 测试ROS到Python映射
        self.assertIn(float, _msg_converter_back)
        self.assertIn(Float64, _msg_converter_back)
        self.assertIn(String, _msg_converter_back)
        self.assertIn(Point, _msg_converter_back)


class TestFieldMapping(unittest.TestCase):
    """测试字段映射"""

    def test_compare_model_fields(self):
        """测试模型字段比较"""
        # Point3D和TestPointModel有相同的字段
        self.assertTrue(compare_model_fields(Point3D, TestPointModel))

        # 与其他类型比较
        self.assertFalse(compare_model_fields(Point3D, TestMappingModel))
        self.assertFalse(compare_model_fields(Point3D, float))

        # 类型对象和实例对象比较
        point = Point3D(x=1.0, y=2.0, z=3.0)
        self.assertTrue(compare_model_fields(Point3D, type(point)))

    def test_set_msg_data(self):
        """测试设置消息数据类型"""
        # 测试float转换
        float_value = "3.14"
        self.assertEqual(set_msg_data("float", float_value), 3.14)
        self.assertEqual(set_msg_data("double", float_value), 3.14)

        # 测试int转换
        int_value = "42"
        self.assertEqual(set_msg_data("int", int_value), 42)

        # 测试bool转换
        bool_value = "True"
        self.assertEqual(set_msg_data("bool", bool_value), True)

        # 测试str转换
        str_value = "hello"
        self.assertEqual(set_msg_data("str", str_value), "hello")

        # 测试默认转换
        default_value = 123
        self.assertEqual(set_msg_data("unknown_type", default_value), "123")


if __name__ == "__main__":
    unittest.main()
