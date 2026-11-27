"""转台的机构动作；零导入。转速为标称估计（每 90° 约 1.2 s），不是硬件标定。"""

PI = 3.141592653589793
SECONDS_PER_QUARTER_TURN = 1.2
MINIMUM_SECONDS = 0.6


class Turntable:
    def rotate(self, joints, angle_deg):
        """转到指定角度（度）；目标超出资产声明的转角范围时由 OS 拒绝"""
        target = angle_deg * PI / 180.0
        travel = abs(target - joints.position("rotate_joint"))
        duration = max(MINIMUM_SECONDS, travel / (PI / 2) * SECONDS_PER_QUARTER_TURN)
        return {"move": {"rotate_joint": target}, "duration": duration, "state": {"angle_deg": angle_deg}}
