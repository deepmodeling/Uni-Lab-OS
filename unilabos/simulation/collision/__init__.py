"""OS 通用碰撞查询：与机构种类无关，三级检查（包围盒 → 保守外包 → 精确 + 最小间隙）。

* :mod:`.profile` 读取碰撞 profile（资产管线生成的 ``collision/{exact,outer}`` 或 ``collision_snapshot`` 导出）；
* :mod:`.instance` 把 profile 放到 OS 装配出的实例位姿上；
* :mod:`.kernel` / :mod:`.refined` 是碰撞内核（需要 ``python-fcl``，延迟导入）；
* :mod:`.mechanism` 把任意机构的关节值变成刚体位姿；
* :mod:`.query` 对一段轨迹逐点检查，并写成 OS 轨迹回执（``collision_checked``）。

机器人领域库只做逆解与路径搜索，碰撞场景与内核从这里取。
"""

from .geometry import IDENTITY, Body, Geometry, PackedRows
from .instance import CollisionInstance
from .mechanism import Attachment, Joint, KinematicChain, Mechanism
from .profile import CollisionProfile
from .query import CollisionQuery, densify, receipt, states_digest, states_from_motion_feed


def __getattr__(name):
    # 内核依赖 python-fcl，按需导入；只用 profile / 运动链的调用方不需要安装它。
    if name in ("SceneChecker", "joint_samples"):
        from . import kernel

        return getattr(kernel, name)
    if name == "RefinedChecker":
        from .refined import RefinedChecker

        return RefinedChecker
    raise AttributeError(name)


__all__ = ["IDENTITY", "Body", "Geometry", "PackedRows", "CollisionInstance", "CollisionProfile", "Attachment", "Joint",
           "KinematicChain", "Mechanism", "CollisionQuery", "densify", "receipt", "states_digest",
           "states_from_motion_feed", "SceneChecker", "RefinedChecker", "joint_samples"]
