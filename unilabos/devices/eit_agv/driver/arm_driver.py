# coding:utf-8
"""
功能:
    AGV机械臂驱动模块, 封装DucoCobot机械臂的核心功能
    主要用于托盘搬运和物料转移任务
"""

import logging
from .DucoCobot.DucoCobot import DucoCobot
from .DucoCobot.gen_py.robot.ttypes import Op, PointOp

# 配置日志
logger = logging.getLogger(__name__)


class ArmDriver:
    """
    功能:
        AGV机械臂驱动类, 提供托盘搬运和物料转移所需的核心功能
    """

    def __init__(self, ip=None, port=None, timeout=60000):
        """
        功能:
            初始化机械臂驱动
        参数:
            ip: 机械臂IP地址, 默认使用配置文件中的值
            port: 机械臂端口, 默认使用配置文件中的值
            timeout: socket超时时间, 单位毫秒, 默认60000ms(60秒)
        """
        self.robot = DucoCobot(ip, port, timeout)
        self.is_connected = False
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.current_gripper = "gripper_type_a"  # 当前安装的夹爪名称,目前夹爪有问题，先默认是type as

        # 运动参数最大值配置
        self.max_joint_velocity = 2.5 * 3.14159  # 最大关节角速度 rad/s
        self.max_joint_acceleration = 3.93  # 最大关节角加速度 rad/s²
        self.max_linear_velocity = 2  # 最大末端线速度 m/s
        self.max_linear_acceleration = 2  # 最大末端线加速度 m/s²
        logger.debug("AGV机械臂驱动初始化完成")

    # ==================== 连接管理 ====================

    def connect(self):
        """
        功能:
            连接机械臂
        返回:
            bool, True表示连接成功, False表示连接失败
        """
        result = self.robot.open()
        if result == 0:
            self.is_connected = True
            logger.debug("机械臂连接成功")
            return True
        else:
            self.is_connected = False
            logger.error("机械臂连接失败")
            return False

    def disconnect(self):
        """
        功能:
            断开机械臂连接
        返回:
            bool, True表示断开成功, False表示断开失败
        """
        result = self.robot.close()
        if result == 0:
            self.is_connected = False
            logger.debug("机械臂断开连接成功")
            return True
        else:
            logger.error("机械臂断开连接失败")
            return False

    def reconnect(self):
        """
        功能:
            重新连接机械臂
        返回:
            bool, True表示重连成功, False表示重连失败
        """
        logger.warning("尝试重新连接机械臂")
        # 先断开旧连接
        try:
            self.robot.close()
        except Exception:
            pass

        # 重新创建连接对象
        self.robot = DucoCobot(self.ip, self.port, self.timeout)

        # 尝试连接
        return self.connect()

    # ==================== 电源和使能控制 ====================

    def power_on(self, block=True):
        """
        功能:
            机械臂上电
        参数:
            block: 是否阻塞, True表示阻塞执行, False表示非阻塞
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("机械臂上电")
        return self.robot.power_on(block)

    def power_off(self, block=True):
        """
        功能:
            机械臂下电
        参数:
            block: 是否阻塞, True表示阻塞执行, False表示非阻塞
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("机械臂下电")
        return self.robot.power_off(block)

    def enable(self, block=True):
        """
        功能:
            机械臂上使能
        参数:
            block: 是否阻塞, True表示阻塞执行, False表示非阻塞
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("机械臂上使能")
        return self.robot.enable(block)

    def disable(self, block=True):
        """
        功能:
            机械臂下使能
        参数:
            block: 是否阻塞, True表示阻塞执行, False表示非阻塞
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("机械臂下使能")
        return self.robot.disable(block)

    # ==================== Op 和 PointOp 辅助方法 ====================

    def create_op(self,
              time_or_dist_1=0, trig_io_1=0, trig_value_1=False,
              trig_time_1=0.0, trig_dist_1=0.0, trig_event_1="",
              time_or_dist_2=0, trig_io_2=0, trig_value_2=False,
              trig_time_2=0.0, trig_dist_2=0.0, trig_event_2="",
              time_or_dist_3=0, trig_io_3=0, trig_value_3=False,
              trig_time_3=0.0, trig_dist_3=0.0, trig_event_3=""):
        """
        功能:
            创建Op对象, 用于定义运动过程中的触发事件(轨迹起点/终点/暂停或停止)
        参数(按协议枚举):
            time_or_dist_x:
                0: 不启用
                1: 时间触发
                2: 距离触发 (第3组目前协议说明不支持距离触发)
            trig_io_x:
                触发IO或寄存器索引号；不启用时建议填0作为占位
            trig_value_x:
                IO时False=低电平, True=高电平；寄存器时会按类型转换后输出
            trig_time_x:
                时间触发参数(ms)；不启用时建议填0
            trig_dist_x:
                距离触发参数(m)；不启用时建议填0 (第3组无效)
            trig_event_x:
                自定义事件名；若不为空则优先触发事件，不触发IO/寄存器；默认必须为空字符串
        返回:
            Op对象
        """
        return Op(
            time_or_dist_1, trig_io_1, trig_value_1, trig_time_1, trig_dist_1, trig_event_1,
            time_or_dist_2, trig_io_2, trig_value_2, trig_time_2, trig_dist_2, trig_event_2,
            time_or_dist_3, trig_io_3, trig_value_3, trig_time_3, trig_dist_3, trig_event_3
        )

    def create_point_op(self, pose, op=None):
        """
        功能:
            创建PointOp对象, 用于定义带触发事件的运动点
        参数:
            pose: 位姿[x,y,z,rx,ry,rz], 位置单位mm, 姿态单位rad
            op: Op对象, 定义该点的触发事件, 为None时使用默认Op
        返回:
            PointOp对象
        """
        if op is None:
            op = self.create_op()
        # 将位置从mm转换为m
        pose_m = [pose[0]/1000.0, pose[1]/1000.0, pose[2]/1000.0, pose[3], pose[4], pose[5]]
        return PointOp(pose_m, op)

    # ==================== 基础运动控制 ====================
    def move_to_joints(self, joints_list, v, a, r=0, block=True, def_acc=False, retry=True):
        """
        功能:
            控制机械臂从当前状态按照关节运动方式移动到目标关节角状态
        参数:
            joints_list: 目标关节角度列表[q1,q2,q3,q4,q5,q6], 范围[-2*PI, 2*PI]即[-6.28, 6.28], 单位rad
            v: 速度百分比, 范围(0, 1], 相对于最大关节角速度的比例
            a: 加速度百分比, 范围(0, 1], 相对于最大关节角加速度的比例
            r: 轨迹融合半径, 单位m, 默认值为0表示无融合. 当数值大于0时表示与下一条运动融合, 产生融合运动时机器人程序会根据融合预读取设定参数在运动指令执行过程中尝试提前获取后续运动函数
            block: 是否阻塞型指令, True表示阻塞执行, False表示非阻塞立即返回
            def_acc: 是否使用默认加速度, 默认为False. 当开启默认加速度时执行运动时最大加速度参数不再生效, 系统将会根据实际工况计算机器人可以产生的最大加速度指令进行运动从而提升节拍
            retry: 连接断开时是否自动重试, 默认True
        返回:
            阻塞执行时返回任务结束状态(无融合为Finished, 有融合为Interrupt), 非阻塞执行时返回任务ID, 用户可以调用get_task_state(id)函数查询当前任务的执行状态
        """
        import math

        # 复制关节角度列表, 避免修改原始数据
        adjusted_joints = list(joints_list)

        # 检查并调整j6角度到合理范围[-4.3, 1.7]
        if adjusted_joints[5] > 1.7:
            # j6超过上限, 减去2π
            adjusted_joints[5] -= 2 * math.pi
            logger.debug(f"j6角度超过上限, 调整前: {joints_list[5]:.3f} rad, 调整后: {adjusted_joints[5]:.3f} rad")
        elif adjusted_joints[5] < -4.3:
            # j6低于下限, 加上2π
            adjusted_joints[5] += 2 * math.pi
            logger.debug(f"j6角度低于下限, 调整前: {joints_list[5]:.3f} rad, 调整后: {adjusted_joints[5]:.3f} rad")

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_joint_velocity
        actual_a = a * self.max_joint_acceleration
        logger.debug(f"关节运动到目标位置: {adjusted_joints}, 速度: {v:.1%} ({actual_v:.3f} rad/s), 加速度: {a:.1%} ({actual_a:.3f} rad/s²)")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.movej2(adjusted_joints, actual_v, actual_a, r, block, op, def_acc)

    def move_to_pose(self, pose, v, a, r=0, q_near=None, tool="", wobj="", block=True, def_acc=False):
        """
        功能:
            控制机械臂从当前状态按照各关节相位同步运动方式移动到末端目标位姿
        参数:
            pose: 目标机器人工具在参考机器人工件坐标系中的位姿[x,y,z,rx,ry,rz], 位置单位mm, 姿态以Rx,Ry,Rz表示范围[-2*PI, 2*PI]即[-6.28, 6.28], 单位rad
            v: 速度百分比, 范围(0, 1], 相对于最大关节角速度的比例
            a: 加速度百分比, 范围(0, 1], 相对于最大关节角加速度的比例
            r: 轨迹融合半径, 单位mm, 默认值为0表示无融合. 当数值大于0时表示与下一条运动融合, 产生融合运动时机器人程序会根据融合预读取设定参数在运动指令执行过程中尝试提前获取后续运动函数
            q_near: 目标点附近位置对应的关节角度, 用于确定逆运动学选解, 为空时使用当前位置
            tool: 工具坐标系名称, 为空时使用当前工具
            wobj: 工件坐标系名称, 为空时使用当前工件坐标系
            block: 是否阻塞执行, True表示等待运动完成, False表示立即返回
            def_acc: 是否使用默认加速度, 默认为False. 当开启默认加速度时执行运动时最大加速度参数不再生效, 系统将会根据实际工况计算机器人可以产生的最大加速度指令进行运动从而提升节拍
        返回:
            阻塞执行时返回任务结束状态(无融合为Finished, 有融合为Interrupt), 非阻塞执行时返回任务ID, 用户可以调用get_task_state(id)函数查询当前任务的执行状态
        """
        # 将位置从mm转换为m
        pose_m = [pose[0]/1000.0, pose[1]/1000.0, pose[2]/1000.0, pose[3], pose[4], pose[5]]
        r_m = r / 1000.0  # 融合半径从mm转换为m

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_joint_velocity
        actual_a = a * self.max_joint_acceleration
        logger.debug(f"关节运动到目标位姿: {pose}mm, 速度: {v:.1%} ({actual_v:.3f} rad/s), 加速度: {a:.1%} ({actual_a:.3f} rad/s²)")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.movej_pose2(pose_m, actual_v, actual_a, r_m, q_near, tool, wobj, block, op, def_acc)

    def move_linear(self, pose, v, a, r=0, q_near=None, tool="", wobj="", block=True, def_acc=False):
        """
        功能:
            控制机械臂末端从当前状态按直线路径移动到目标位姿
        参数:
            pose: 目标工具在工件坐标系下的位姿[x,y,z,rx,ry,rz], 位置单位mm, 姿态范围[-2*PI, 2*PI] rad
            v: 速度百分比, 范围(0, 1], 相对于最大末端线速度的比例
            a: 加速度百分比, 范围(0, 1], 相对于最大末端线加速度的比例
            r: 轨迹融合半径, 单位mm, 0 表示无融合, 大于 0 时与下一条运动融合
            q_near: 目标点附近的关节角度, 用于校验逆运动学选解空间, None 时使用当前关节
            tool: 使用的工具名称, 为空时默认当前工具
            wobj: 使用的工件坐标系名称, 为空时默认当前工件坐标系
            block: 指令是否阻塞, False 表示非阻塞立即返回
            def_acc: 是否使用默认加速度, True 时忽略 a 由系统计算可用最大加速度
        返回:
            阻塞执行返回任务结束状态(Finished 无融合或 Interrupt 有融合), 非阻塞执行返回任务ID, 可调用 get_task_state(id) 查询状态
        """
        # 将位置从mm转换为m
        pose_m = [pose[0]/1000.0, pose[1]/1000.0, pose[2]/1000.0, pose[3], pose[4], pose[5]]
        r_m = r / 1000.0  # 融合半径从mm转换为m

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_linear_velocity
        actual_a = a * self.max_linear_acceleration
        logger.debug(f"直线运动到目标位姿: {pose}mm, 速度: {v:.1%} ({actual_v:.3f} m/s), 加速度: {a:.1%} ({actual_a:.3f} m/s²)")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.movel(pose_m, actual_v, actual_a, r_m, q_near, tool, wobj, block, op, def_acc)

    def movec(self, p1, p2, v, a, r=0, mode=0, q_near=None, tool="", wobj="", block=True, def_acc=True, arc_rad=0):
        """
        功能:
            控制机械臂做圆弧运动, 起始点为当前位姿点, 途径p1点, 终点为p2点
        参数:
            p1: 圆弧运动过程中任意机器人工具在参考机器人工件坐标系中的位姿中间点, 位置单位mm, 姿态以Rx,Ry,Rz表示范围[-2*PI, 2*PI], 单位rad
            p2: 目标机器人工具在参考机器人工件坐标系中的位姿, 位置单位mm, 姿态以Rx,Ry,Rz表示范围[-2*PI, 2*PI], 单位rad
            v: 速度百分比, 范围(0, 1], 相对于最大末端线速度的比例
            a: 加速度百分比, 范围(0, 1], 相对于最大末端线加速度的比例
            r: 轨迹融合半径, 单位mm, 默认值为0, 表示无融合. 当数值大于0时表示与下一条运动融合. 产生融合运动时, 机器人程序会根据融合预读取设定参数在运动指令执行过程中尝试提前获取后续运动函数
            mode: 姿态控制模式
                0: 姿态与终点保持一致, 即机器人会以p2点的姿态为目标姿态, 平滑运动到目标姿态
                1: 姿态与起点保持一致, 即机器人会以开始执行movec函数时机器人末端工具坐标系在工件坐标系中的姿态为准, 始终保持该姿态值
                2: 姿态受圆心约束, 即机器人会以开始执行movec函数时机器人末端工具坐标系与目标圆弧路径起点处切线方向间关系为参考, 在圆弧运动过程中始终保持末端工具与圆弧实时运动所处位置切线方向参考关系
            q_near: 目标点附近位置对应的关节角度, 用于校验机器人运动过程中逆运动学选解空间
            tool: 设置使用的工具的名称, 为空时默认为当前使用的工具
            wobj: 设置使用的工件坐标系的名称, 为空时默认为当前使用的工件坐标系
            block: 指令是否阻塞型指令, 如果为False表示非阻塞指令, 指令会立即返回
            def_acc: 是否使用默认加速度, 默认为True. 当开启默认加速度时, 执行运动时最大加速度参数不再生效, 系统将会根据实际工况计算机器人可以产生的最大加速度指令进行运动, 从而提升节拍
            arc_rad: 圆弧半径, 单位mm
        返回:
            当配置为阻塞执行, 返回值代表当前任务结束时的状态, 若无融合为Finished, 若有融合为Interrupt. 当配置为非阻塞执行, 返回值代表当前任务的id信息, 用户可以调用get_noneblock_taskstate(id)函数查询当前任务的执行状态
        """
        # 将位置从mm转换为m
        p1_m = [p1[0]/1000.0, p1[1]/1000.0, p1[2]/1000.0, p1[3], p1[4], p1[5]]
        p2_m = [p2[0]/1000.0, p2[1]/1000.0, p2[2]/1000.0, p2[3], p2[4], p2[5]]
        r_m = r / 1000.0  # 融合半径从mm转换为m
        arc_rad_m = arc_rad / 1000.0  # 圆弧半径从mm转换为m

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_linear_velocity
        actual_a = a * self.max_linear_acceleration
        logger.debug(f"圆弧运动: p1={p1}mm, p2={p2}mm, 速度: {v:.1%} ({actual_v:.3f} m/s), 加速度: {a:.1%} ({actual_a:.3f} m/s²), 模式: {mode}")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.movec(p1_m, p2_m, actual_v, actual_a, r_m, mode, q_near, tool, wobj, block, op, def_acc, arc_rad_m)

    def move_circle(self, p1, p2, v, a, rad=0, mode=1, q_near=None, tool="", wobj="", block=True, def_acc=True):
        """
        功能:
            控制机械臂做圆周运动, 起始点为当前位姿点, 途径p1点和p2点
        参数:
            p1: 圆周运动过程中任意机器人工具在参考机器人工件坐标系中的位姿中间点1, 位置单位mm, 姿态以Rx,Ry,Rz表示范围[-2*PI, 2*PI], 单位rad
            p2: 圆周运动过程中任意机器人工具在参考机器人工件坐标系中的位姿中间点2, 最终以机器人初始运动位置-p1-p2的顺序决定最终整圆轨迹, 位置单位mm, 姿态以Rx,Ry,Rz表示范围[-2*PI, 2*PI], 单位rad
            v: 速度百分比, 范围(0, 1], 相对于最大末端线速度的比例
            a: 加速度百分比, 范围(0, 1], 相对于最大末端线加速度的比例
            rad: 轨迹融合半径, 单位mm, 默认值为0, 表示无融合. 当数值大于0时表示与下一条运动融合. 产生融合运动时, 机器人程序会根据融合预读取设定参数在运动指令执行过程中尝试提前获取后续运动函数
            mode: 姿态控制模式
                1: 姿态与起点保持一致, 即机器人会以开始执行movec函数时机器人末端工具坐标系在工件坐标系中的姿态为准, 始终保持该姿态值
                2: 姿态受圆心约束, 即机器人会以开始执行movec函数时机器人末端工具坐标系与目标圆弧路径起点处切线方向间关系为参考, 在圆弧运动过程中始终保持末端工具与圆弧实时运动所处位置切线方向参考关系
            q_near: 目标点附近位置对应的关节角度, 用于校验机器人运动过程中逆运动学选解空间
            tool: 设置使用的工具的名称, 默认为当前使用的工具
            wobj: 设置使用的工件坐标系的名称, 默认为当前使用的工件坐标系
            block: 指令是否阻塞型指令, 如果为False表示非阻塞指令, 指令会立即返回, 默认为阻塞
            def_acc: 是否使用默认加速度, 默认为True. 当开启默认加速度时, 执行运动时最大加速度参数不再生效, 系统将会根据实际工况计算机器人可以产生的最大加速度指令进行运动, 从而提升节拍
        返回:
            当配置为阻塞执行, 返回值代表当前任务结束时的状态, 若无融合为Finished, 若有融合为Interrupt. 当配置为非阻塞执行, 返回值代表当前任务的id信息, 用户可以调用get_noneblock_taskstate(id)函数查询当前任务的执行状态
        """
        # 将位置从mm转换为m
        p1_m = [p1[0]/1000.0, p1[1]/1000.0, p1[2]/1000.0, p1[3], p1[4], p1[5]]
        p2_m = [p2[0]/1000.0, p2[1]/1000.0, p2[2]/1000.0, p2[3], p2[4], p2[5]]
        rad_m = rad / 1000.0  # 融合半径从mm转换为m

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_linear_velocity
        actual_a = a * self.max_linear_acceleration
        logger.debug(f"圆周运动: p1={p1}mm, p2={p2}mm, 速度: {v:.1%} ({actual_v:.3f} m/s), 加速度: {a:.1%} ({actual_a:.3f} m/s²), 模式: {mode}")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.move_circle(p1_m, p2_m, actual_v, actual_a, rad_m, mode, q_near, tool, wobj, block, op, def_acc)

    def move_tcp_offset(self, pose_offset, v, a, r=0, tool="", block=True, def_acc=False):
        """
        功能:
            控制机械臂沿工具坐标系直线移动一个增量. 偏移量将会转换为齐次变换矩阵右乘于当前机器人末端位姿之上
        参数:
            pose_offset: pose数据类型或长度为6的number型数组, 表示工具坐标系下的位姿偏移量[x,y,z,rx,ry,rz], 位置单位mm, 姿态单位rad
            v: 速度百分比, 范围(0, 1], 相对于最大末端线速度的比例, 当x、y、z均为0时, 线速度按比例换算成角速度
            a: 加速度百分比, 范围(0, 1], 相对于最大末端线加速度的比例
            r: 轨迹融合半径, 单位mm, 默认值为0, 表示无融合. 当数值大于0时表示与下一条运动融合. 产生融合运动时, 机器人程序会根据融合预读取设定参数在运动指令执行过程中尝试提前获取后续运动函数
            tool: 设置使用的工具的名称, 为空时默认为当前使用的工具
            block: 指令是否阻塞型指令, 如果为False表示非阻塞指令, 指令会立即返回
            def_acc: 是否使用默认加速度, 默认为False. 当开启默认加速度时, 执行运动时最大加速度参数不再生效, 系统将会根据实际工况计算机器人可以产生的最大加速度指令进行运动, 从而提升节拍
        返回:
            当配置为阻塞执行, 返回值代表当前任务结束时的状态, 若无融合为Finished, 若有融合为Interrupt. 当配置为非阻塞执行, 返回值代表当前任务的id信息, 用户可以调用get_noneblock_taskstate(id)函数查询当前任务的执行状态
        """
        # 将位置从mm转换为m
        pose_offset_m = [pose_offset[0]/1000.0, pose_offset[1]/1000.0, pose_offset[2]/1000.0,
                         pose_offset[3], pose_offset[4], pose_offset[5]]
        r_m = r / 1000.0  # 融合半径从mm转换为m

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_linear_velocity
        actual_a = a * self.max_linear_acceleration
        logger.debug(f"沿工具坐标系移动: {pose_offset}mm, 速度: {v:.1%} ({actual_v:.3f} m/s), 加速度: {a:.1%} ({actual_a:.3f} m/s²)")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.tcp_move(pose_offset_m, actual_v, actual_a, r_m, tool, block, op, def_acc)

    def move_tcp_2p(self, p1, p2, v, a, r=0, tool="", wobj="", block=True, def_acc=False):
        """
        功能:
            控制机器人沿工具坐标系直线移动一个增量, 增量为p1与p2点之间的差, 运动的目标点为: 当前点*p1^-1*p2
        参数:
            p1: 表示工具坐标系下的位姿偏移量计算点1[x,y,z,rx,ry,rz], 位置单位mm, 姿态单位rad
            p2: 表示工具坐标系下的位姿偏移量计算点2[x,y,z,rx,ry,rz], 位置单位mm, 姿态单位rad
            v: 速度百分比, 范围(0, 1], 相对于最大末端线速度的比例, 当x、y、z均为0时, 线速度按比例换算成角速度
            a: 加速度百分比, 范围(0, 1], 相对于最大末端线加速度的比例
            r: 轨迹融合半径, 单位mm, 默认值为0, 表示无融合. 当数值大于0时表示与下一条运动融合. 产生融合运动时, 机器人程序会根据融合预读取设定参数在运动指令执行过程中尝试提前获取后续运动函数
            tool: 设置使用的工具的名称, 默认为当前使用的工具
            wobj: 设置使用的工件坐标系的名称, 默认为当前使用的工件坐标系
            block: 指令是否阻塞型指令, 如果为False表示非阻塞指令, 指令会立即返回
            def_acc: 是否使用默认加速度, 默认为False. 当开启默认加速度时, 执行运动时最大加速度参数不再生效, 系统将会根据实际工况计算机器人可以产生的最大加速度指令进行运动, 从而提升节拍
        返回:
            当配置为阻塞执行, 返回值代表当前任务结束时的状态, 若无融合为Finished, 若有融合为Interrupt. 当配置为非阻塞执行, 返回值代表当前任务的id信息, 用户可以调用get_noneblock_taskstate(id)函数查询当前任务的执行状态
        """
        # 将位置从mm转换为m
        p1_m = [p1[0]/1000.0, p1[1]/1000.0, p1[2]/1000.0, p1[3], p1[4], p1[5]]
        p2_m = [p2[0]/1000.0, p2[1]/1000.0, p2[2]/1000.0, p2[3], p2[4], p2[5]]
        r_m = r / 1000.0  # 融合半径从mm转换为m

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_linear_velocity
        actual_a = a * self.max_linear_acceleration
        logger.debug(f"沿工具坐标系移动(两点法): p1={p1}mm, p2={p2}mm, 速度: {v:.1%} ({actual_v:.3f} m/s), 加速度: {a:.1%} ({actual_a:.3f} m/s²)")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.tcp_move_2p(p1_m, p2_m, actual_v, actual_a, r_m, tool, wobj, block, op, def_acc)

    def move_wobj_offset(self, pose_offset, v, a, r=0, wobj="", block=True, def_acc=False):
        """
        功能:
            控制机械臂沿工件坐标系直线移动一个增量
        参数:
            pose_offset: pose数据类型或长度为6的number型数组, 表示工件坐标系下的位姿偏移量[x,y,z,rx,ry,rz], 位置单位mm, 姿态单位rad
            v: 速度百分比, 范围(0, 1], 相对于最大末端线速度的比例, 当x、y、z均为0时, 线速度按比例换算成角速度
            a: 加速度百分比, 范围(0, 1], 相对于最大末端线加速度的比例
            r: 轨迹融合半径, 单位mm, 默认值为0, 表示无融合. 当数值大于0时表示与下一条运动融合. 产生融合运动时, 机器人程序会根据融合预读取设定参数在运动指令执行过程中尝试提前获取后续运动函数
            wobj: 设置使用的工件的名称, 为空时默认为当前使用的工件
            block: 指令是否阻塞型指令, 如果为False表示非阻塞指令, 指令会立即返回
            def_acc: 是否使用默认加速度, 默认为False. 当开启默认加速度时, 执行运动时最大加速度参数不再生效, 系统将会根据实际工况计算机器人可以产生的最大加速度指令进行运动, 从而提升节拍
        返回:
            当配置为阻塞执行, 返回值代表当前任务结束时的状态, 若无融合为Finished, 若有融合为Interrupt. 当配置为非阻塞执行, 返回值代表当前任务的id信息, 用户可以调用get_noneblock_taskstate(id)函数查询当前任务的执行状态
        """
        # 将位置从mm转换为m
        pose_offset_m = [pose_offset[0]/1000.0, pose_offset[1]/1000.0, pose_offset[2]/1000.0,
                         pose_offset[3], pose_offset[4], pose_offset[5]]
        r_m = r / 1000.0  # 融合半径从mm转换为m

        # 将百分比转换为实际速度和加速度值
        actual_v = v * self.max_linear_velocity
        actual_a = a * self.max_linear_acceleration
        logger.debug(f"沿工件坐标系移动: {pose_offset}mm, 速度: {v:.1%} ({actual_v:.3f} m/s), 加速度: {a:.1%} ({actual_a:.3f} m/s²)")
        op = self.create_op()  # 创建完整的Op对象，这个必须有
        return self.robot.wobj_move(pose_offset_m, actual_v, actual_a, r_m, wobj, block, op, def_acc)

    # ==================== 任务控制 ====================
    def run_program(self, program_name, block=True):
        """
        功能:
            运行程序脚本
        参数:
            program_name: 脚本程序名称
            block: 是否阻塞执行, True表示等待程序执行完成, False表示立即返回
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.debug(f"运行程序脚本: {program_name}")
        return self.robot.run_program(program_name, block)

    def stop(self, block=True):
        """
        功能:
            停止所有任务
        参数:
            block: 是否阻塞执行
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.warning("停止所有任务")
        return self.robot.stop(block)

    def pause(self, block=True):
        """
        功能:
            暂停所有任务
        参数:
            block: 是否阻塞执行
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("暂停所有任务")
        return self.robot.pause(block)

    def resume(self, block=True):
        """
        功能:
            恢复所有暂停的任务
        参数:
            block: 是否阻塞执行
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("恢复所有任务")
        return self.robot.resume(block)

    # ==================== 坐标系设置 ====================

    def set_tool(self, name, tool_offset, payload, inertia_tensor):
        """
        功能:
            设置工具坐标系参数
        参数:
            name: 工具坐标系名称
            tool_offset: 工具TCP偏移量[x,y,z,rx,ry,rz], 单位mm, rad
            payload: 末端负载[mass,x_cog,y_cog,z_cog], 单位kg, mm
            inertia_tensor: 惯量矩阵参数[xx,xy,xz,yy,yz,zz], 单位kg*m^2
        返回:
            任务结束时的状态
        """
        # 将工具偏移量从mm转换为m
        tool_offset_m = [tool_offset[0]/1000.0, tool_offset[1]/1000.0, tool_offset[2]/1000.0,
                         tool_offset[3], tool_offset[4], tool_offset[5]]
        # 将负载质心从mm转换为m
        payload_m = [payload[0], payload[1]/1000.0, payload[2]/1000.0, payload[3]/1000.0]

        logger.info(f"设置工具坐标系: {name}")
        return self.robot.set_tool_data(name, tool_offset_m, payload_m, inertia_tensor)

    def set_workobject(self, name, wobj):
        """
        功能:
            设置工件坐标系
        参数:
            name: 工件坐标系名称
            wobj: 工件坐标系相对于基坐标系的位姿[x,y,z,rx,ry,rz], 单位mm, rad
        返回:
            任务结束时的状态
        """
        # 将位置从mm转换为m
        wobj_m = [wobj[0]/1000.0, wobj[1]/1000.0, wobj[2]/1000.0, wobj[3], wobj[4], wobj[5]]

        logger.info(f"设置工件坐标系: {name}")
        return self.robot.set_wobj(name, wobj_m)

    def get_tcp_offset(self):
        """
        功能:
            获取当前TCP偏移量
        返回:
            TCP偏移量[x,y,z,rx,ry,rz], 单位mm, rad
        """
        offset_m = self.robot.get_tcp_offset()
        # 将位置从m转换为mm
        if offset_m is not None and len(offset_m) >= 6:
            return [offset_m[0]*1000.0, offset_m[1]*1000.0, offset_m[2]*1000.0,
                    offset_m[3], offset_m[4], offset_m[5]]
        return offset_m

    def set_wobj_offset(self, wobj, active=True):
        """
        功能:
            设置工件坐标系偏移量
        参数:
            wobj: 工件坐标系偏移量[x,y,z,rx,ry,rz], 单位mm, rad
            active: 是否启用
        返回:
            任务结束时的状态
        """
        # 将位置从mm转换为m
        wobj_m = [wobj[0]/1000.0, wobj[1]/1000.0, wobj[2]/1000.0, wobj[3], wobj[4], wobj[5]]

        logger.info(f"设置工件坐标系偏移: {wobj}mm, 启用: {active}")
        return self.robot.set_wobj_offset(wobj_m, active)

    # ==================== 运动学计算 ====================

    def calculate_forward_kinematics(self, joints_position, tool="", wobj=""):
        """
        功能:
            计算正运动学, 根据关节角度计算末端位姿
        参数:
            joints_position: 关节角度列表, 单位rad
            tool: 工具坐标系名称, 为空时使用当前工具
            wobj: 工件坐标系名称, 为空时使用当前工件坐标系
        返回:
            末端位姿[x,y,z,rx,ry,rz], 单位m, rad
        """
        pose_m = self.robot.cal_fkine(joints_position, tool, wobj)
        return pose_m

    def calculate_inverse_kinematics(self, pose, q_near=None, tool="", wobj=""):
        """
        功能:
            计算逆运动学, 根据末端位姿计算关节角度
        参数:
            pose: 末端位姿[x,y,z,rx,ry,rz], 单位m, rad
            q_near: 参考关节角度, 用于选解, 为空时使用当前关节角度
            tool: 工具坐标系名称, 为空时使用当前工具
            wobj: 工件坐标系名称, 为空时使用当前工件坐标系
        返回:
            关节角度列表[q1,q2,q3,q4,q5,q6], 单位rad
        """
        return self.robot.cal_ikine(pose, q_near, tool, wobj)

    # ==================== 状态查询 ====================

    def get_robot_state(self):
        """
        功能:
            获取机器人状态
        返回:
            状态列表[机器人状态, 程序状态, 安全控制器状态, 操作模式]
        """
        return self.robot.get_robot_state()

    def get_tcp_pose(self):
        """
        功能:
            获取当前TCP位姿
        返回:
            TCP位姿[x,y,z,rx,ry,rz], 单位mm, rad
        """
        pose_m = self.robot.get_tcp_pose()
        # 将位置从m转换为mm
        if pose_m is not None and len(pose_m) >= 6:
            return [pose_m[0]*1000.0, pose_m[1]*1000.0, pose_m[2]*1000.0,
                    pose_m[3], pose_m[4], pose_m[5]]
        return pose_m

    def get_joints_position(self):
        """
        功能:
            获取当前关节角度
        返回:
            关节角度列表[q1,q2,q3,q4,q5,q6], 单位rad
        """
        return self.robot.get_actual_joints_position()

    def get_tcp_speed(self):
        """
        功能:
            获取当前TCP速度
        返回:
            TCP速度列表, 单位m/s, rad/s
        """
        return self.robot.get_tcp_speed()

    def get_tcp_force(self):
        """
        功能:
            获取当前末端力矩信息
        返回:
            末端力矩[Fx,Fy,Fz,Mx,My,Mz], 单位N, N.m
        """
        return self.robot.get_tcp_force()

    def is_moving(self):
        """
        功能:
            判断机械臂是否在运动
        返回:
            bool, True表示正在运动, False表示静止
        """
        return self.robot.robotmoving()

    # ==================== IO控制 ====================

    def set_digital_output(self, num, value, block=True):
        """
        功能:
            设置控制柜数字IO输出
        参数:
            num: IO输出口序号, 范围1-16
            value: True为高电平, False为低电平
            block: 是否阻塞执行
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info(f"设置控制柜IO{num}输出: {value}")
        return self.robot.set_standard_digital_out(num, value, block)

    def set_tool_digital_output(self, num, value, block=True):
        """
        功能:
            设置末端数字IO输出
        参数:
            num: 末端IO输出口序号, 范围1-2
            value: True为高电平, False为低电平
            block: 是否阻塞执行
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info(f"设置末端IO{num}输出: {value}")
        return self.robot.set_tool_digital_out(num, value, block)

    def get_digital_input(self, num):
        """
        功能:
            读取控制柜数字IO输入
        参数:
            num: IO输入口序号, 范围1-16
        返回:
            bool, True为高电平, False为低电平
        """
        return self.robot.get_standard_digital_in(num)

    def get_tool_digital_input(self, num):
        """
        功能:
            读取末端数字IO输入
        参数:
            num: 末端IO输入口序号, 范围1-2
        返回:
            bool, True为高电平, False为低电平
        """
        return self.robot.get_tool_digital_in(num)

    # ==================== 快换和夹爪控制 ====================

    def release_quick_change(self, block=True):
        """
        功能:
            松开快换装置
        参数:
            block: 是否阻塞执行, True表示等待完成, False表示立即返回
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("松开快换装置")
        return self.robot.set_standard_digital_out(1, True, block)

    def lock_quick_change(self, block=True):
        """
        功能:
            夹紧快换装置
        参数:
            block: 是否阻塞执行, True表示等待完成, False表示立即返回
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.info("夹紧快换装置")
        return self.robot.set_standard_digital_out(1, False, block)

    def open_gripper(self, block=True):
        """
        功能:
            张开夹爪
        参数:
            block: 是否阻塞执行, True表示等待完成, False表示立即返回
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.debug("张开夹爪")
        return self.robot.set_standard_digital_out(2, True, block)

    def close_gripper(self, block=True):
        """
        功能:
            闭合夹爪
        参数:
            block: 是否阻塞执行, True表示等待完成, False表示立即返回
        返回:
            阻塞执行返回任务状态, 非阻塞执行返回任务ID
        """
        logger.debug("闭合夹爪")
        return self.robot.set_standard_digital_out(2, False, block)

    # ==================== 夹爪状态检测 ====================

    def get_gripper_state(self):
        """
        功能:
            获取夹爪当前状态
        返回:
            str, 夹爪状态:
                "opened": 张开到位
                "gripped": 夹紧到位(夹到物料)
                "empty": 空夹(未夹到物料)
                "unknown": 未知状态
        """
        di10 = self.robot.get_standard_digital_in(10)
        di11 = self.robot.get_standard_digital_in(11)

        if di10 == True and di11 == False:
            # 张开到位 或 空夹(未夹到物料), 两者DI信号相同
            logger.info("夹爪状态: 张开到位/空夹")
            return "opened"
        elif di10 == False and di11 == True:
            logger.info("夹爪状态: 夹紧到位(夹到物料)")
            return "gripped"
        else:
            logger.warning(f"夹爪状态未知: DI10={di10}, DI11={di11}")
            return "unknown"

    def is_gripper_opened(self):
        """
        功能:
            检测夹爪是否张开到位
        返回:
            bool, True表示张开到位, False表示未张开
        """
        di10 = self.robot.get_standard_digital_in(10)
        di11 = self.robot.get_standard_digital_in(11)
        return di10 == True and di11 == False

    def is_gripper_gripped(self):
        """
        功能:
            检测夹爪是否夹紧到位(夹到物料)
        返回:
            bool, True表示夹紧到位且夹到物料, False表示未夹紧或未夹到物料
        """
        di10 = self.robot.get_standard_digital_in(10)
        di11 = self.robot.get_standard_digital_in(11)
        return di10 == False and di11 == True

    def is_gripper_empty(self):
        """
        功能:
            检测夹爪是否空夹(闭合但未夹到物料)
            注意: 空夹和张开到位的DI信号相同, 需要结合实际操作流程判断
        返回:
            bool, True表示空夹, False表示非空夹状态
        """
        di10 = self.robot.get_standard_digital_in(10)
        di11 = self.robot.get_standard_digital_in(11)
        # 空夹信号与张开到位相同, 返回相同结果
        return di10 == True and di11 == False

    # ==================== 料盘检测 ====================

    def check_slot_has_material(self, slot_num):
        """
        功能:
            检测指定料位是否有料
        参数:
            slot_num: 料位编号, 范围1-7
                1-3: 快换料位(bg1-bg3, 对应DI3-DI5)
                4-7: 托盘料位(bg4-bg7, 对应DI6-DI9)
        返回:
            bool, True表示有料, False表示无料
        """
        if slot_num < 1 or slot_num > 7:
            logger.error(f"无效的料位编号: {slot_num}, 有效范围1-7")
            return False

        # 料位编号与DI端口的映射: slot_num + 2 = DI端口号
        di_port = slot_num + 2
        di_value = self.robot.get_standard_digital_in(di_port)

        # DI为false表示有料, true表示无料
        has_material = not di_value
        slot_type = "快换" if slot_num <= 3 else "托盘"
        logger.info(f"料位bg{slot_num}({slot_type})检测: {'有料' if has_material else '无料'}")
        return has_material

    def check_quick_change_slot(self, slot_num):
        """
        功能:
            检测快换料位是否有料
        参数:
            slot_num: 快换料位编号, 范围1-3(对应bg1-bg3)
        返回:
            bool, True表示有料, False表示无料
        """
        if slot_num < 1 or slot_num > 3:
            logger.error(f"无效的快换料位编号: {slot_num}, 有效范围1-3")
            return False
        return self.check_slot_has_material(slot_num)

    def check_tray_slot(self, slot_num):
        """
        功能:
            检测托盘料位是否有料
        参数:
            slot_num: 托盘料位编号, 范围1-4(对应bg4-bg7)
        返回:
            bool, True表示有料, False表示无料
        """
        if slot_num < 1 or slot_num > 4:
            logger.error(f"无效的托盘料位编号: {slot_num}, 有效范围1-4")
            return False
        # 托盘料位1-4对应bg4-bg7
        return self.check_slot_has_material(slot_num + 3)

    def get_all_slots_status(self):
        """
        功能:
            获取所有料位的状态
        返回:
            dict, 包含所有料位状态:
                {
                    "quick_change": [bg1, bg2, bg3],  # 快换料位状态, True有料/False无料
                    "tray": [bg4, bg5, bg6, bg7]      # 托盘料位状态, True有料/False无料
                }
        """
        quick_change_status = []
        tray_status = []

        # 检测快换料位 bg1-bg3 (DI3-DI5)
        for i in range(1, 4):
            di_value = self.robot.get_standard_digital_in(i + 2)
            quick_change_status.append(not di_value)

        # 检测托盘料位 bg4-bg7 (DI6-DI9)
        for i in range(4, 8):
            di_value = self.robot.get_standard_digital_in(i + 2)
            tray_status.append(not di_value)

        result = {
            "quick_change": quick_change_status,
            "tray": tray_status
        }

        logger.info(f"所有料位状态: 快换{quick_change_status}, 托盘{tray_status}")
        return result

    # ==================== 夹爪状态管理 ====================

    def get_current_gripper(self):
        """
        功能:
            获取当前安装的夹爪名称
        返回:
            str或None, 当前夹爪名称, 未安装时返回None
        """
        return self.current_gripper

    def set_current_gripper(self, gripper_name):
        """
        功能:
            设置当前安装的夹爪名称(仅更新状态, 不执行物理操作)
        参数:
            gripper_name: 夹爪名称, 为None表示未安装夹爪
        """
        self.current_gripper = gripper_name
        if gripper_name is not None:
            logger.info(f"当前夹爪已设置为: {gripper_name}")
        else:
            logger.info("当前夹爪已清除")

    # ==================== 负载管理 ====================

    def set_payload(self, mass, x_cog, y_cog, z_cog):
        """
        功能:
            设置抓取负载参数
        参数:
            mass: 负载质量, 范围[0, 35], 单位kg
            x_cog: 质心x坐标, 相对于工具坐标系, 单位mm
            y_cog: 质心y坐标, 相对于工具坐标系, 单位mm
            z_cog: 质心z坐标, 相对于工具坐标系, 单位mm
        返回:
            任务结束时的状态
        """
        # 将质心坐标从mm转换为m
        x_cog_m = x_cog / 1000.0
        y_cog_m = y_cog / 1000.0
        z_cog_m = z_cog / 1000.0

        logger.info(f"设置负载: 质量={mass}kg, 质心=({x_cog},{y_cog},{z_cog})mm")
        return self.robot.set_load_data([mass, x_cog_m, y_cog_m, z_cog_m])

    # ==================== 速度控制 ====================

    def set_max_joint_velocity(self, velocity):
        """
        功能:
            设置最大关节角速度
        参数:
            velocity: 最大关节角速度, 单位rad/s
        """
        self.max_joint_velocity = velocity
        logger.info(f"设置最大关节角速度: {velocity:.3f} rad/s")

    def set_max_joint_acceleration(self, acceleration):
        """
        功能:
            设置最大关节角加速度
        参数:
            acceleration: 最大关节角加速度, 单位rad/s²
        """
        self.max_joint_acceleration = acceleration
        logger.info(f"设置最大关节角加速度: {acceleration:.3f} rad/s²")

    def set_max_linear_velocity(self, velocity):
        """
        功能:
            设置最大末端线速度
        参数:
            velocity: 最大末端线速度, 单位m/s
        """
        self.max_linear_velocity = velocity
        logger.info(f"设置最大末端线速度: {velocity:.3f} m/s")

    def set_max_linear_acceleration(self, acceleration):
        """
        功能:
            设置最大末端线加速度
        参数:
            acceleration: 最大末端线加速度, 单位m/s²
        """
        self.max_linear_acceleration = acceleration
        logger.info(f"设置最大末端线加速度: {acceleration:.3f} m/s²")

    def get_max_velocities(self):
        """
        功能:
            获取当前最大速度配置
        返回:
            dict, 包含最大速度配置:
                {
                    "joint_velocity": 最大关节角速度(rad/s),
                    "joint_acceleration": 最大关节角加速度(rad/s²),
                    "linear_velocity": 最大末端线速度(m/s),
                    "linear_acceleration": 最大末端线加速度(m/s²)
                }
        """
        return {
            "joint_velocity": self.max_joint_velocity,
            "joint_acceleration": self.max_joint_acceleration,
            "linear_velocity": self.max_linear_velocity,
            "linear_acceleration": self.max_linear_acceleration
        }

    def set_speed_ratio(self, ratio):
        """
        功能:
            设置全局速度比例
        参数:
            ratio: 速度比例, 范围(0,100], 单位%
        返回:
            任务结束时的状态
        """
        logger.info(f"设置全局速度比例: {ratio}%")
        return self.robot.speed(ratio)

    # ==================== 碰撞检测 ====================

    def set_collision_level(self, level):
        """
        功能:
            设置碰撞检测等级
        参数:
            level: 碰撞检测等级, 0:关闭, 1-5:等级1到5
        返回:
            任务结束时的状态
        """
        logger.info(f"设置碰撞检测等级: {level}")
        return self.robot.collision_detect(level)

    # ==================== 错误处理 ====================

    def get_last_error(self):
        """
        功能:
            获取最后一次错误信息
        返回:
            错误信息
        """
        return self.robot.get_last_error()

    def clear_error(self):
        """
        功能:
            清除错误信息
        返回:
            任务结束时的状态
        """
        logger.info("清除错误信息")
        return self.robot.clear_error_message()

    # ==================== 任务状态查询 ====================

    def get_task_state(self, task_id):
        """
        功能:
            查询非阻塞任务的执行状态
        参数:
            task_id: 任务ID
        返回:
            任务状态
        """
        return self.robot.get_noneblock_taskstate(task_id)

    # ==================== 系统变量管理 ====================

    def get_system_value_double(self, name):
        """
        功能:
            获取number类型系统变量
        参数:
            name: 系统变量名称
        返回:
            float, number类型系统变量的值
        """
        logger.debug(f"获取系统变量(double): {name}")
        return self.robot.get_system_value_double(name)

    def get_system_value_lists(self, name):
        """
        功能:
            获取pose_list/joint_list类型系统变量
        参数:
            name: 系统变量名称
        返回:
            list, pose_list或joint_list类型系统变量的值
        """
        logger.debug(f"获取系统变量(lists): {name}")
        return self.robot.get_system_value_lists(name)


def main():
    """
    功能:
        交互式测试机械臂驱动的主要接口
    """
    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("机械臂驱动交互式测试程序")
    print("=" * 60)

    # 初始化机械臂驱动
    arm = ArmDriver()

    # 连接机械臂
    print("\n正在连接机械臂...")
    if not arm.connect():
        print("连接失败, 程序退出")
        return

    # 上电
    print("\n正在上电...")
    result = arm.power_on(block=True)
    print(f"上电结果: {result}")

    # 使能
    print("\n正在使能...")
    result = arm.enable(block=True)
    print(f"使能结果: {result}")

    # 获取当前状态
    print("\n当前机械臂状态:")
    state = arm.get_robot_state()
    print(f"  机器人状态: {state}")
    joints = arm.get_joints_position()
    print(f"  当前关节角度: {joints}")
    pose = arm.get_tcp_pose()
    print(f"  当前TCP位姿: {pose}")

    # 交互式测试循环
    while True:
        print("\n" + "=" * 60)
        print("请选择要测试的功能:")
        print("1. 测试 move_to_joints (关节运动)")
        print("2. 测试 move_to_pose (位姿运动)")
        print("3. 测试 run_program (运行程序)")
        print("4. 查看当前状态")
        print("5. 测试快换控制")
        print("6. 测试夹爪控制")
        print("7. 查看夹爪状态")
        print("8. 查看料盘状态")
        print("0. 退出程序")
        print("=" * 60)

        choice = input("请输入选项 (0-8): ").strip()

        if choice == "1":
            # 测试关节运动
            print("\n--- 测试关节运动 ---")
            print("请输入目标关节角度 (6个值, 单位rad, 用逗号分隔)")
            print("有效范围: 每个关节角度范围[-2*PI, 2*PI] rad, 即[-6.28, 6.28]")
            print("示例: 0,-0.5,0.5,0,0.5,0")
            joints_input = input("关节角度: ").strip()

            try:
                joints_list = [float(x) for x in joints_input.split(',')]
                if len(joints_list) != 6:
                    print("错误: 必须输入6个关节角度值")
                    continue

                v = input("速度百分比 (范围0-1, 例如0.3表示30%, 默认0.5): ").strip()
                v = float(v) if v else 0.5

                a = input("加速度百分比 (范围0-1, 例如0.5表示50%, 默认0.5): ").strip()
                a = float(a) if a else 0.5

                print(f"\n开始运动到关节位置: {joints_list}")
                result = arm.move_to_joints(joints_list, v=v, a=a, block=True)
                print(f"运动结果: {result}")

                # 显示运动后的位置
                current_joints = arm.get_joints_position()
                print(f"运动后关节角度: {current_joints}")

            except ValueError:
                print("错误: 输入格式不正确")
            except Exception as e:
                print(f"错误: {e}")

        elif choice == "2":
            # 测试位姿运动
            print("\n--- 测试位姿运动 ---")
            print("请输入目标位姿 (6个值: x,y,z,rx,ry,rz, 位置单位m, 姿态单位rad, 用逗号分隔)")
            print("示例: 0.3,0.2,0.4,3.14,0,0")
            pose_input = input("目标位姿: ").strip()

            try:
                pose = [float(x) for x in pose_input.split(',')]
                if len(pose) != 6:
                    print("错误: 必须输入6个位姿值")
                    continue

                v = input("速度百分比 (范围0-1, 例如0.3表示30%, 默认0.5): ").strip()
                v = float(v) if v else 0.5

                a = input("加速度百分比 (范围0-1, 例如0.5表示50%, 默认0.5): ").strip()
                a = float(a) if a else 0.5

                print(f"\n开始运动到目标位姿: {pose}")
                result = arm.move_to_pose(pose, v=v, a=a, block=True)
                print(f"运动结果: {result}")

                # 显示运动后的位置
                current_pose = arm.get_tcp_pose()
                print(f"运动后TCP位姿: {current_pose}")

            except ValueError:
                print("错误: 输入格式不正确")
            except Exception as e:
                print(f"错误: {e}")

        elif choice == "3":
            # 测试运行程序
            print("\n--- 测试运行程序 ---")
            program_name = input("请输入程序名称: ").strip()

            if not program_name:
                print("错误: 程序名称不能为空")
                continue

            try:
                print(f"\n开始运行程序: {program_name}")
                result = arm.run_program(program_name, block=True)
                print(f"程序运行结果: {result}")

            except Exception as e:
                print(f"错误: {e}")

        elif choice == "4":
            # 查看当前状态
            print("\n--- 当前机械臂状态 ---")
            try:
                state = arm.get_robot_state()
                print(f"机器人状态: {state}")

                joints = arm.get_joints_position()
                print(f"当前关节角度: {joints}")

                pose = arm.get_tcp_pose()
                print(f"当前TCP位姿: {pose}")

                is_moving = arm.is_moving()
                print(f"是否在运动: {is_moving}")

                tcp_speed = arm.get_tcp_speed()
                print(f"TCP速度: {tcp_speed}")

            except Exception as e:
                print(f"错误: {e}")

        elif choice == "5":
            # 测试快换控制
            print("\n--- 测试快换控制 ---")
            print("1. 松开快换")
            print("2. 夹紧快换")
            sub_choice = input("请选择操作 (1-2): ").strip()

            try:
                if sub_choice == "1":
                    print("正在松开快换...")
                    result = arm.release_quick_change(block=True)
                    print(f"松开快换结果: {result}")
                elif sub_choice == "2":
                    print("正在夹紧快换...")
                    result = arm.lock_quick_change(block=True)
                    print(f"夹紧快换结果: {result}")
                else:
                    print("无效的选项")
            except Exception as e:
                print(f"错误: {e}")

        elif choice == "6":
            # 测试夹爪控制
            print("\n--- 测试夹爪控制 ---")
            print("1. 张开夹爪")
            print("2. 闭合夹爪")
            sub_choice = input("请选择操作 (1-2): ").strip()

            try:
                if sub_choice == "1":
                    print("正在张开夹爪...")
                    result = arm.open_gripper(block=True)
                    print(f"张开夹爪结果: {result}")
                elif sub_choice == "2":
                    print("正在闭合夹爪...")
                    result = arm.close_gripper(block=True)
                    print(f"闭合夹爪结果: {result}")
                else:
                    print("无效的选项")
            except Exception as e:
                print(f"错误: {e}")

        elif choice == "7":
            # 查看夹爪状态
            print("\n--- 夹爪状态 ---")
            try:
                state = arm.get_gripper_state()
                print(f"夹爪状态: {state}")

                is_opened = arm.is_gripper_opened()
                print(f"是否张开到位: {is_opened}")

                is_gripped = arm.is_gripper_gripped()
                print(f"是否夹紧到位(有物料): {is_gripped}")

            except Exception as e:
                print(f"错误: {e}")

        elif choice == "8":
            # 查看料盘状态
            print("\n--- 料盘状态 ---")
            print("1. 查看所有料位状态")
            print("2. 查看指定快换料位")
            print("3. 查看指定托盘料位")
            sub_choice = input("请选择操作 (1-3): ").strip()

            try:
                if sub_choice == "1":
                    status = arm.get_all_slots_status()
                    print("\n快换料位状态 (bg1-bg3):")
                    for i, has_material in enumerate(status["quick_change"], 1):
                        print(f"  bg{i}: {'有料' if has_material else '无料'}")
                    print("\n托盘料位状态 (bg4-bg7):")
                    for i, has_material in enumerate(status["tray"], 4):
                        print(f"  bg{i}: {'有料' if has_material else '无料'}")

                elif sub_choice == "2":
                    slot_num = input("请输入快换料位编号 (1-3): ").strip()
                    slot_num = int(slot_num)
                    has_material = arm.check_quick_change_slot(slot_num)
                    print(f"快换料位bg{slot_num}: {'有料' if has_material else '无料'}")

                elif sub_choice == "3":
                    slot_num = input("请输入托盘料位编号 (1-4, 对应bg4-bg7): ").strip()
                    slot_num = int(slot_num)
                    has_material = arm.check_tray_slot(slot_num)
                    print(f"托盘料位bg{slot_num + 3}: {'有料' if has_material else '无料'}")

                else:
                    print("无效的选项")
            except ValueError:
                print("错误: 请输入有效的数字")
            except Exception as e:
                print(f"错误: {e}")

        elif choice == "0":
            # 退出程序
            print("\n正在退出...")
            break

        else:
            print("无效的选项, 请重新输入")

    # # 下使能和下电
    # print("\n正在下使能...")
    # arm.disable(block=True)

    # print("正在下电...")
    # arm.power_off(block=True)

    # 断开连接
    print("正在断开连接...")
    arm.disconnect()

    print("\n程序已退出")


if __name__ == "__main__":
    main()
