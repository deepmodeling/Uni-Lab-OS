"""低温冰箱的机构动作；零导入。行程取资产关节声明，时长为标称估计，不是硬件标定。"""


class Freezer:
    def open_door(self, joints):
        """开门：抽屉伸到资产声明的最大行程"""
        return {"move": {"Joints_drawer_joint": joints.upper("Joints_drawer_joint")}, "duration": 4.0,
                "state": {"door": "open"}}

    def close_door(self, joints):
        """关门：抽屉收回"""
        return {"move": {"Joints_drawer_joint": joints.lower("Joints_drawer_joint")}, "duration": 4.0,
                "state": {"door": "closed"}}
