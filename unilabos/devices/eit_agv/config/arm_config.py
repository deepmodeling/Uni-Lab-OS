# arm_config.py
# -*- coding: utf-8 -*-
"""
机械臂驱动配置文件
"""

# 机械臂控制器连接配置
ARM_HOST = "192.168.1.10"
ARM_PORT = 7003
ARM_TIMEOUT = 3.0

# 夹爪控制配置
# 是否启用夹持到检测功能: True表示夹爪闭合后检测是否夹到物料, False表示不检测
ENABLE_GRIP_DETECTION = True
