"""
测试运行器

运行所有消息转换器的测试用例。
"""

import unittest
import sys
import os

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

# 导入测试模块
from test.ros.msgs.test_basic import TestBasicFunctionality
from test.ros.msgs.test_conversion import TestBasicConversion, TestMappingConversion
from test.ros.msgs.test_mapping import TestTypeMapping, TestFieldMapping


def run_tests():
    """运行所有测试"""
    # 创建测试加载器
    loader = unittest.TestLoader()

    # 创建测试套件
    suite = unittest.TestSuite()

    # 添加测试类
    suite.addTests(loader.loadTestsFromTestCase(TestBasicFunctionality))
    suite.addTests(loader.loadTestsFromTestCase(TestBasicConversion))
    suite.addTests(loader.loadTestsFromTestCase(TestMappingConversion))
    suite.addTests(loader.loadTestsFromTestCase(TestTypeMapping))
    suite.addTests(loader.loadTestsFromTestCase(TestFieldMapping))

    # 创建测试运行器
    runner = unittest.TextTestRunner(verbosity=2)

    # 运行测试
    result = runner.run(suite)

    # 返回结果
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_tests()
    sys.exit(not success)
