"""
基本测试

测试消息转换器的基本功能，包括导入、类型映射等。
"""

import unittest

from unilabos.ros.msgs.message_converter import (
    msg_converter_manager,
    get_msg_type,
    get_action_type,
    get_ros_type_by_msgname,
    Point3D,
    Point,
    Float64,
    String,
    Bool,
    Int32,
)


class TestBasicFunctionality(unittest.TestCase):
    """测试消息转换器的基本功能"""

    def test_manager_initialization(self):
        """测试导入管理器初始化"""
        self.assertIsNotNone(msg_converter_manager)
        self.assertTrue(len(msg_converter_manager.list_modules()) > 0)
        self.assertTrue(len(msg_converter_manager.list_classes()) > 0)

    def test_get_msg_type(self):
        """测试获取消息类型"""
        self.assertEqual(get_msg_type(float), Float64)
        self.assertEqual(get_msg_type(str), String)
        self.assertEqual(get_msg_type(bool), Bool)
        self.assertEqual(get_msg_type(int), Int32)
        self.assertEqual(get_msg_type(Point3D), Point)

        # 测试错误情况
        with self.assertRaises(ValueError):
            get_msg_type(set)  # 不支持的类型

    def test_get_action_type(self):
        """测试获取动作类型"""
        float_action = get_action_type(float)
        self.assertIsNotNone(float_action)
        self.assertTrue("type" in float_action)
        self.assertTrue("goal" in float_action)
        self.assertTrue("feedback" in float_action)

        # 测试错误情况
        with self.assertRaises(ValueError):
            get_action_type(set)  # 不支持的类型

    def test_get_ros_type_by_msgname(self):
        """测试通过消息名称获取ROS类型"""
        # 测试有效的消息名称
        point_type = get_ros_type_by_msgname("geometry_msgs/msg/Point")
        self.assertEqual(point_type, Point)

        # 测试无效的消息名称
        with self.assertRaises(ValueError):
            get_ros_type_by_msgname("invalid_format")

        # 不存在的消息类型可能会引发ImportError，但这依赖于运行环境
        # 因此不进行显式测试


if __name__ == "__main__":
    unittest.main()
