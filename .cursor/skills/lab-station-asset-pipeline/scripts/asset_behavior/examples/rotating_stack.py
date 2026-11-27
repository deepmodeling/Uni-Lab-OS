"""旋转堆栈（70 位 + 固定交接位）的机构动作；零导入。动作名与 pylabrobot_unilab 存储类适配器（Liconic）一致：
``fetch_plate_to_loading_tray(site)`` 把库位上的板送到交接位，``take_in_plate(site)`` 从交接位收进库位。

``site`` 是资产库位名 ``column_NN/level_N``，角度和高度都从库位位置算，不查表。
铲板没有在资产里声明库位，升降零位时铲板托面的高度用下面的候选常数（沿用原模拟器的映射，未实测）。
时长为标称估计。物料从库位到交接位的归属变化由动作合同声明、OS 库存结算；这里的 carry 只是画面提示。
"""

PI = 3.141592653589793
FORK_ALONG_AT_ZERO = -0.0576   # 候选：升降关节为 0 时铲板托面沿升降轴的坐标（米）
LIFT_SECONDS = 3.0
DWELL_SECONDS = 2.0
PORT = "interaction_site"


def _turn(joints, sites, slot):
    """把这一列转到交接口方向：走最短的那一边。"""
    delta = sites.angle_about("carousel_joint", PORT) - sites.angle_about("carousel_joint", slot)
    while delta > PI:
        delta -= 2 * PI
    while delta <= -PI:
        delta += 2 * PI
    duration = max(1.0, abs(delta) / (PI / 2) * 2.0)
    return {"move": {"carousel_joint": joints.position("carousel_joint") + delta}, "duration": duration}


def _lift(sites, name):
    return {"move": {"plate_lift_joint": sites.along("plate_lift_joint", name) - FORK_ALONG_AT_ZERO},
            "duration": LIFT_SECONDS}


class RotatingStack:
    def fetch_plate_to_loading_tray(self, joints, sites, site):
        """出库：转到库位所在的列，升降到那一层取板，送到固定交接位"""
        return [_turn(joints, sites, site), _lift(sites, site), {"duration": DWELL_SECONDS}, _lift(sites, PORT),
                {"duration": DWELL_SECONDS, "carry": {"from": site, "to": PORT}, "state": {"last_site": site}}]

    def take_in_plate(self, joints, sites, site):
        """入库：从固定交接位取板，转到库位所在的列，升降到那一层放下"""
        return [_lift(sites, PORT), {"duration": DWELL_SECONDS}, _turn(joints, sites, site), _lift(sites, site),
                {"duration": DWELL_SECONDS, "carry": {"from": PORT, "to": site}, "state": {"last_site": site}},
                _lift(sites, PORT)]
