"""ROS 2 传输层测试只在装有 rclpy 的环境收集；HostLink-only 环境整体跳过而不是导入失败。"""

import importlib.util

collect_ignore_glob = [] if importlib.util.find_spec("rclpy") else ["test_*.py", "msgs/test_*.py"]
