"""SO503 下门的机构动作：左右两扇门叶；零导入。开合角度取资产关节声明，时长为标称估计。"""


class LowerDoor:
    ACTION_CONTRACTS = {"open_door": {"estimate_duration_fixed": 3.0}, "close_door": {"estimate_duration_fixed": 3.0}}

    def open_door(self, joints):
        """打开两扇门叶到资产声明的最大角度"""
        return {"move": {name: joints.upper(name) for name in ("leaf_l_joint", "leaf_r_joint")}, "duration": 3.0,
                "state": {"door": "open"}}

    def close_door(self, joints):
        """关上两扇门叶"""
        return {"move": {name: joints.lower(name) for name in ("leaf_l_joint", "leaf_r_joint")}, "duration": 3.0,
                "state": {"door": "closed"}}
