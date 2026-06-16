# config.py
# -*- coding: utf-8 -*-
"""
AGV驱动配置文件
"""

# AGV控制器连接配置
AGV_HOST = "192.168.1.5"
AGV_PORT = 19204  # 查询命令端口
AGV_PORT_NAVIGATION = 19206  # 路径导航命令端口
AGV_TIMEOUT = 3.0

# 协议常量
FRAME_HEAD_SIZE = 16

# 任务状态查询命令 (厂家示例代码中的1110 = 0x0456)
REQ_CMD_ROBOT_STATUS_TASK = 0x03FC  # 任务状态查询请求
RSP_CMD_ROBOT_STATUS_TASK = 0x2B0C  # 任务状态查询响应
REQ_CMD_TASK_QUERY = 0x0456  # 厂家示例: 任务查询命令 (1110)

# 路径导航命令
REQ_CMD_ROBOT_TASK_GOTARGET = 0x0BEB  # 路径导航命令
RSP_CMD_ROBOT_TASK_GOTARGET = 0x32FB  # 路径导航响应

# 机器人位置查询命令
REQ_CMD_ROBOT_STATUS_LOC = 0x03EC  # 机器人位置查询请求 (1004)
RSP_CMD_ROBOT_STATUS_LOC = 0x2AFC  # 机器人位置查询响应 (11004)

# 机器人电池状态查询命令
REQ_CMD_ROBOT_STATUS_BATTERY = 0x03EF  # 电池状态查询请求 (1007)
RSP_CMD_ROBOT_STATUS_BATTERY = 0x2AFF  # 电池状态查询响应 (11007)

# 任务状态映射
TASK_STATUS_MAP = {
    0: "NONE",
    1: "WAITING",
    2: "RUNNING",
    3: "SUSPENDED",
    4: "COMPLETED",
    5: "FAILED",
    6: "CANCELED",
}

# 任务类型映射
TASK_TYPE_MAP = {
    0: "NO_NAV",
    1: "FREE_NAV_TO_POINT",
    2: "FREE_NAV_TO_SITE",
    3: "PATH_NAV_TO_SITE",
    7: "TRANSLATE_ROTATE",
    100: "OTHER",
}

# AGV运动参数配置
AGV_MAX_SPEED = 0.3  # 最大速度, 单位m/s
AGV_MAX_WSPEED = 0.8  # 最大角速度, 单位rad/s
AGV_MAX_ACC = 0.2  # 最大加速度, 单位m/s^2
AGV_MAX_WACC = 0.5  # 最大角加速度, 单位rad/s^2

# AGV查询重试配置
AGV_QUERY_MAX_RETRIES = 3       # 查询最大重试次数
AGV_QUERY_RETRY_DELAY = 1.0    # 首次重试等待时间, 单位秒, 后续翻倍(指数退避)

# 工站位置配置
STATION_POSITIONS = {
    "LM1": {
        "name": "synthesis_station",
        "description": "合成工站位置"
    },
    "LM2": {
        "name": "analysis_station",
        "description": "分析工站位置"
    },
    "CP6": {
        "name": "charging_station",
        "description": "充电站位置"
    },
    "LM3": {
        "name": "concentration_station",
        "description": "浓缩工站位置"
    },
    "LM4": {
        "name": "shelf",
        "description": "货架位置"
    },
    "LM0": {
        "name": "maintenance_point",
        "description": "检修点位置"
    },
    "PP5": {
        "name": "charging_transition_point",
        "description": "充电过渡点位置"
    }
}

