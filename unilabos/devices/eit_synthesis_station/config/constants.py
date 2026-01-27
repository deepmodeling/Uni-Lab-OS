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




