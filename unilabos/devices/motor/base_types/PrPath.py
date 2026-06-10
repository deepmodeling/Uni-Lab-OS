from enum import Enum


class PR_PATH(Enum):
    # TYPE (Bit0-3)
    TYPE_NO_ACTION = 0x0000  # 无动作
    TYPE_POSITIONING = 0x0001  # 位置定位
    TYPE_VELOCITY = 0x0002  # 速度运行
    TYPE_HOME = 0x0003  # 回零

    # INS (Bit4) - 默认都是插断模式
    INS_INTERRUPT = 0x0010  # 插断

    # OVLP (Bit5) - 默认都是不重叠
    OVLP_NO_OVERLAP = 0x0000  # 不重叠

    # POSITION MODE (Bit6)
    POS_ABSOLUTE = 0x0000  # 绝对位置
    POS_RELATIVE = 0x0040  # 相对位置

    # MOTOR MODE (Bit7)
    MOTOR_ABSOLUTE = 0x0000  # 绝对电机
    MOTOR_RELATIVE = 0x0080  # 相对电机

    # 常用组合（默认都是插断且不重叠）
    # 位置定位相关
    ABS_POS = TYPE_POSITIONING | INS_INTERRUPT | OVLP_NO_OVERLAP | POS_ABSOLUTE  # 绝对定位
    REL_POS = TYPE_POSITIONING | INS_INTERRUPT | OVLP_NO_OVERLAP | POS_RELATIVE  # 相对定位

    # 速度运行相关
    VELOCITY = TYPE_VELOCITY | INS_INTERRUPT | OVLP_NO_OVERLAP  # 速度模式

    # 回零相关
    HOME = TYPE_HOME | INS_INTERRUPT | OVLP_NO_OVERLAP  # 回零模式

    # 电机模式组合
    ABS_POS_REL_MOTOR = ABS_POS | MOTOR_RELATIVE  # 绝对定位+相对电机
    REL_POS_REL_MOTOR = REL_POS | MOTOR_RELATIVE  # 相对定位+相对电机
