from enum import IntEnum


class DeviceCode(IntEnum):
    """
    功能:
        工站设备代码枚举.
    """
    ARM = 301                    # 机械臂
    POWDER_DISPENSER = 304       # 加粉模块
    MAG_STIRRER = 305            # 热磁力搅拌模块
    CAP_OPENER = 303             # 开盖模块
    W1_SHELF = 342               # W1排货架
    W1_1_2 = 360                 # W-1-1、W-1-2
    W1_3_4 = 361                 # W-1-3、W-1-4
    W1_5_6 = 362                 # W-1-5、W-1-6
    W1_7_8 = 363                 # W-1-7、W-1-8
    TRANSFER_SHELF = 346         # 中转货架
    OUTER_DOOR = 340             # 过渡舱外门
    INNER_DOOR = 341             # 过渡舱内门
    MAGNET_ADDER = 343           # 加磁子模块
    FLASH_FILTER = 336           # 闪滤模块
    EXCHANGE_SHELF = 351         # 交换仓货架
    GLOVEBOX_ENV = 352           # 手套箱箱体环境

class TaskStatus(IntEnum):
    """
    功能:
        任务状态码枚举, 用于轮询与判定.
    备注:
        主要来自补充文档的状态码说明, 未覆盖的状态可按实际扩展.
    """
    UNSTARTED = 0  # 未开始
    RUNNING = 1  # 运行中
    COMPLETED = 2  # 已完成
    PAUSED = 3  # 已暂停
    FAILED = 4  # 失败
    STOPPED = 5  # 已停止
    PAUSING = 6  # 暂停中
    STOPPING = 7  # 停止中
    WAITING = 8  # 等待中
    HOLDING = 10  # 挂起/保持

class StationState(IntEnum):
    """
    功能:
        工站设备状态码枚举, 与任务状态类似但语义是整站状态.
    """
    IDLE = 0  # 空闲/待机
    RUNNING = 1  # 运行中
    PAUSED = 3  # 已暂停
    PAUSING = 6  # 暂停中
    STOPPING = 7  # 停止中
    HOLDING = 10  # 挂起/保持

class DeviceModuleStatus(IntEnum):
    """
    功能:
        工站设备模块状态码枚举.
    """

    AVAILABLE = 0  # 可用/就绪
    RUNNING = 1  # 运行中
    UNAVAILABLE = 2  # 不可用
    OPEN = 3  # 打开
    CLOSE = 4  # 关闭
    OUTSIDE = 5  # 在外/离位
    HOME = 6  # 原点/回零

class NoticeType(IntEnum):
    """
    功能:
        消息通知类型.
    """

    INFO = 0  # 信息
    FAULT = 1  # 故障
    ALARM = 2  # 告警

class NoticeStatus(IntEnum):
    """
    功能:
        告警状态.
    """

    ABNORMAL = 1  # 异常
    FIXING = 2  # 处理中
    FIXED = 3  # 已恢复

class FaultRecoveryType(IntEnum):
    """
    功能:
        故障恢复处理类型.
    """

    RECOVER = 0  # 恢复
    SKIP_STEP_FAIL = 1  # 跳过步骤（并判定失败）
    SKIP_STEP_SUCCESS = 2  # 跳过步骤（并判定成功）
    SKIP_SAMPLE_ALL = 3  # 跳过整个样品/该样品所有步骤
    RETRY = 4  # 重试
    SKIP_AND_TERMINATE = 5  # 跳过并终止任务

class ResourceCode(IntEnum):
    """
    功能:
        资源码（托盘/载具）枚举.
    备注:
        资源码用于在流程/接口中引用具体耗材托盘或载具类型.
    """

    #---------------------托盘编码-----------------------
    REACTION_TUBE_TRAY_2ML = 201000726  # 2 mL 反应试管托盘
    TEST_TUBE_MAGNET_TRAY_2ML = 201000711  # 2 mL 试管磁子托盘
    REACTION_SEAL_CAP_TRAY = 201000712  # 反应密封盖托盘
    FLASH_FILTER_INNER_BOTTLE_TRAY = 201000727  # 闪滤瓶内瓶托盘
    FLASH_FILTER_OUTER_BOTTLE_TRAY = 201000728  # 闪滤瓶外瓶托盘

    TIP_TRAY_50UL = 201000815  # 50 μL Tip 头托盘
    TIP_TRAY_1ML = 201000731  # 1 mL Tip 头托盘
    TIP_TRAY_5ML = 201000512  # 5 mL Tip 头托盘

    POWDER_BUCKET_TRAY_30ML = 201000600  # 30 mL 粉桶托盘

    REAGENT_BOTTLE_TRAY_2ML = 201000730  # 2 mL 试剂瓶托盘
    REAGENT_BOTTLE_TRAY_8ML = 201000502  # 8 mL 试剂瓶托盘
    REAGENT_BOTTLE_TRAY_40ML = 201000503  # 40 mL 试剂瓶托盘
    REAGENT_BOTTLE_TRAY_125ML = 220000023  # 125 mL 试剂瓶托盘

    #---------------------耗材编码-----------------------
    REACTION_TUBE_2ML = 551000502 # 2 mL 反应试管
    TEST_TUBE_MAGNET_2ML = 220000322  # 2 mL 试管磁子
    REACTION_SEAL_CAP = 211009427  # 反应密封盖
    FLASH_FILTER_INNER_BOTTLE = 220000320  # 闪滤瓶内瓶
    FLASH_FILTER_OUTER_BOTTLE = 220000321  # 闪滤瓶外瓶

    TIP_1ML = 220000308  # 1 mL Tip 头
    TIP_5ML = 214000037  # 5 mL Tip 头
    TIP_50UL = 220000304  # 50 μL Tip 头

    POWDER_BUCKET_30ML = 201000816  # 30 mL 粉桶

    REAGENT_BOTTLE_2ML = 502000353  # 2 mL 试剂瓶
    REAGENT_BOTTLE_8ML = 220000005  # 8 mL 试剂瓶
    REAGENT_BOTTLE_40ML = 220000092  # 40 mL 试剂瓶
    REAGENT_BOTTLE_125ML = 220000008  # 125 mL 试剂瓶

TRAY_CODE_DISPLAY_NAME = {
    int(ResourceCode.REACTION_TUBE_TRAY_2ML): "2 mL反应试管托盘",
    int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML): "2 mL试管磁子托盘",
    int(ResourceCode.REACTION_SEAL_CAP_TRAY): "反应密封盖托盘",
    int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY): "闪滤瓶内瓶托盘",
    int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY): "闪滤瓶外瓶托盘",
    int(ResourceCode.TIP_TRAY_50UL): "50 μL Tip 头托盘",
    int(ResourceCode.TIP_TRAY_1ML): "1 mL Tip 头托盘",
    int(ResourceCode.TIP_TRAY_5ML): "5 mL Tip 头托盘",
    int(ResourceCode.POWDER_BUCKET_TRAY_30ML): "30 mL粉桶托盘",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): "2 mL试剂瓶托盘",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): "8 mL试剂瓶托盘",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): "40 mL试剂瓶托盘",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): "125 mL试剂瓶托盘",
}

# 耗材编码到标准中文名称映射.
CONSUMABLE_CODE_DISPLAY_NAME = {
    int(ResourceCode.TIP_50UL): "50 μL Tip头",
    int(ResourceCode.TIP_1ML): "1 mL Tip头",
    int(ResourceCode.TIP_5ML): "5 mL Tip头",
    int(ResourceCode.TEST_TUBE_MAGNET_2ML): "2 mL反应管磁子",
    int(ResourceCode.REACTION_TUBE_2ML): "2 mL反应管",
    int(ResourceCode.REACTION_SEAL_CAP): "反应密封盖",
    int(ResourceCode.FLASH_FILTER_INNER_BOTTLE): "闪滤瓶内瓶",
    int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE): "闪滤瓶外瓶",
}

# 耗材编码到托盘编码映射.
CONSUMABLE_CODE_TO_TRAY_CODE = {
    int(ResourceCode.TIP_50UL): int(ResourceCode.TIP_TRAY_50UL),
    int(ResourceCode.TIP_1ML): int(ResourceCode.TIP_TRAY_1ML),
    int(ResourceCode.TIP_5ML): int(ResourceCode.TIP_TRAY_5ML),
    int(ResourceCode.TEST_TUBE_MAGNET_2ML): int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML),
    int(ResourceCode.REACTION_TUBE_2ML): int(ResourceCode.REACTION_TUBE_TRAY_2ML),
    int(ResourceCode.REACTION_SEAL_CAP): int(ResourceCode.REACTION_SEAL_CAP_TRAY),
    int(ResourceCode.FLASH_FILTER_INNER_BOTTLE): int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY),
    int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE): int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY),
}

# 归一化后的耗材别名到耗材编码映射.
CONSUMABLE_ALIAS_TO_CODE = {
    # 50 μL Tip头
    "50ul枪头": int(ResourceCode.TIP_50UL),
    "50ul吸头": int(ResourceCode.TIP_50UL),
    "50ultip": int(ResourceCode.TIP_50UL),
    "50ultip头": int(ResourceCode.TIP_50UL),
    "50ultip枪头": int(ResourceCode.TIP_50UL),
    "50ultip吸头": int(ResourceCode.TIP_50UL),
    # 1 mL Tip头
    "1ml枪头": int(ResourceCode.TIP_1ML),
    "1ml吸头": int(ResourceCode.TIP_1ML),
    "1mltip": int(ResourceCode.TIP_1ML),
    "1mltip头": int(ResourceCode.TIP_1ML),
    "1mltip枪头": int(ResourceCode.TIP_1ML),
    "1mltip吸头": int(ResourceCode.TIP_1ML),
    # 5 mL Tip头
    "5ml枪头": int(ResourceCode.TIP_5ML),
    "5ml吸头": int(ResourceCode.TIP_5ML),
    "5mltip": int(ResourceCode.TIP_5ML),
    "5mltip头": int(ResourceCode.TIP_5ML),
    "5mltip枪头": int(ResourceCode.TIP_5ML),
    "5mltip吸头": int(ResourceCode.TIP_5ML),

    # 反应管
    "2ml反应管": int(ResourceCode.REACTION_TUBE_2ML),
    "2ml反应试管": int(ResourceCode.REACTION_TUBE_2ML),
    "反应管": int(ResourceCode.REACTION_TUBE_2ML),
    "反应试管": int(ResourceCode.REACTION_TUBE_2ML),

    # 磁子
    "2ml反应管磁子": int(ResourceCode.TEST_TUBE_MAGNET_2ML),
    "2ml试管磁子": int(ResourceCode.TEST_TUBE_MAGNET_2ML),
    "反应管磁子": int(ResourceCode.TEST_TUBE_MAGNET_2ML),
    "试管磁子": int(ResourceCode.TEST_TUBE_MAGNET_2ML),

    # 反应密封盖
    "反应密封盖": int(ResourceCode.REACTION_SEAL_CAP),
    "反应盖板": int(ResourceCode.REACTION_SEAL_CAP),
    "密封盖": int(ResourceCode.REACTION_SEAL_CAP),
    
    # 闪滤瓶
    "闪滤瓶内瓶": int(ResourceCode.FLASH_FILTER_INNER_BOTTLE),
    "闪滤内瓶": int(ResourceCode.FLASH_FILTER_INNER_BOTTLE),
    "内瓶": int(ResourceCode.FLASH_FILTER_INNER_BOTTLE),
    "闪滤瓶外瓶": int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE),
    "闪滤外瓶": int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE),
    "外瓶": int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE),
}

class TraySpec:
    """
    功能:
        托盘规格，使用 (col, row) 数字表示；行按字母序 A=1, B=2 ... H=8，列保持原数字.
    """

    REAGENT_BOTTLE_TRAY_2ML = (8, 6)   # 2 mL 试剂瓶托盘
    REAGENT_BOTTLE_TRAY_8ML = (4, 3)   # 8 mL 试剂瓶托盘
    REAGENT_BOTTLE_TRAY_40ML = (3, 2)  # 40 mL 试剂瓶托盘
    REAGENT_BOTTLE_TRAY_125ML = (2, 1)  # 125 mL 试剂瓶托盘
    REACTION_TUBE_TRAY_2ML = (6, 4)    # 2 mL 反应试管托盘
    TEST_TUBE_MAGNET_TRAY_2ML = (6, 4) # 2 mL 试管磁子托盘
    REACTION_SEAL_CAP_TRAY = (1, 1)    # 反应密封盖托盘
    FLASH_FILTER_INNER_BOTTLE_TRAY = (8, 6)  # 闪滤瓶内瓶托盘
    FLASH_FILTER_OUTER_BOTTLE_TRAY = (8, 6)  # 闪滤瓶外瓶托盘
    TIP_TRAY_50UL = (12, 8)   # 50 μL Tip 头托盘
    TIP_TRAY_1ML = (12, 8)    # 1 mL Tip 头托盘
    TIP_TRAY_5ML = (6, 4)     # 5 mL Tip 头托盘
    POWDER_BUCKET_TRAY_30ML = (1, 2)   # 30 mL 粉桶托盘


# ===================== AGV 自动下料相关常量 =====================

# 资源码到 AGV 物料类型名称的映射
RESOURCE_CODE_TO_MATERIAL_TYPE = {
    int(ResourceCode.REACTION_TUBE_TRAY_2ML): "REACTION_TUBE_TRAY_2ML",
    int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML): "TEST_TUBE_MAGNET_TRAY_2ML",
    int(ResourceCode.REACTION_SEAL_CAP_TRAY): "REACTION_SEAL_CAP_TRAY",
    int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY): "FLASH_FILTER_INNER_BOTTLE_TRAY",
    int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY): "FLASH_FILTER_OUTER_BOTTLE_TRAY",
    int(ResourceCode.TIP_TRAY_50UL): "TIP_TRAY_50UL",
    int(ResourceCode.TIP_TRAY_1ML): "TIP_TRAY_1ML",
    int(ResourceCode.TIP_TRAY_5ML): "TIP_TRAY_5ML",
    int(ResourceCode.POWDER_BUCKET_TRAY_30ML): "POWDER_BUCKET_TRAY_30ML",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_2ML): "REAGENT_BOTTLE_TRAY_2ML",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_8ML): "REAGENT_BOTTLE_TRAY_8ML",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_40ML): "REAGENT_BOTTLE_TRAY_40ML",
    int(ResourceCode.REAGENT_BOTTLE_TRAY_125ML): "REAGENT_BOTTLE_TRAY_125ML",
}

# 合成工站下料位置(TB-x-x)到 AGV 托盘名称的映射
TB_CODE_TO_SYNTHESIS_TRAY = {
    "TB-1-1": "synthesis_station_tray_1-1",
    "TB-1-2": "synthesis_station_tray_1-2",
    "TB-1-3": "synthesis_station_tray_1-3",
    "TB-1-4": "synthesis_station_tray_1-4",
    "TB-2-1": "synthesis_station_tray_2-1",
    "TB-2-2": "synthesis_station_tray_2-2",
    "TB-2-3": "synthesis_station_tray_2-3",
    "TB-2-4": "synthesis_station_tray_2-4",
}

# 货架托盘位置列表
SHELF_TRAY_POSITIONS = [
    "shelf_tray_1-1", "shelf_tray_1-2", "shelf_tray_1-3", "shelf_tray_1-4",
    "shelf_tray_2-1", "shelf_tray_2-2", "shelf_tray_2-3", "shelf_tray_2-4",
    "shelf_tray_3-1", "shelf_tray_3-2", "shelf_tray_3-3", "shelf_tray_3-4",
]

# 分析工站托盘位置列表
ANALYSIS_STATION_TRAY_POSITIONS = [
    "analysis_station_tray_1-2"
]

# ===================== PLR 资源转移目标设备配置 =====================

# 分析工站 ROS2 设备 ID（需与实际部署拓扑一致）
ANALYSIS_STATION_DEVICE_ID = "eit_analysis_station"

# 货架设备 ID（如果货架没有独立 ROS2 设备节点则设为 None）
SHELF_DEVICE_ID: str | None = None

# 目标设备资源路径（用于 get_resource_with_dir 获取挂载点）
ANALYSIS_STATION_RESOURCE_PATH = "/eit_analysis_station/Analysis_Deck"
SHELF_RESOURCE_PATH: str | None = None




