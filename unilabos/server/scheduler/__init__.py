"""微后端执行协调、HostLink/ROS2 组网和后端调度协议适配。

后端调度器是唯一 DAG、排序和 retry 权威。本包只消费后端命令、选择后端已指定
的 HostLink/ROS2 route、管理执行期错误 gate，并回报状态；不会创建本地待调度
队列，也不会打开旧 inventory/device-state/workflow-history 数据库。
"""
