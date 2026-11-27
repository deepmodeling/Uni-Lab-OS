"""机构测试共用：一个带抽屉、摆臂、未声明行程轴和连续转轴的小装置，以及用它做的资产包。"""
import json
import math

from pxr import Gf, Usd, UsdGeom, UsdPhysics


def _joint(stage, path, kind, body0, body1, axis, lower, upper, pos0=(0, 0, 0)):
    joint = (UsdPhysics.PrismaticJoint if kind == "prismatic" else UsdPhysics.RevoluteJoint).Define(stage, path)
    joint.CreateBody0Rel().SetTargets([body0])
    joint.CreateBody1Rel().SetTargets([body1])
    joint.CreateAxisAttr(axis)
    joint.CreateLowerLimitAttr(lower)
    joint.CreateUpperLimitAttr(upper)
    joint.CreateLocalPos0Attr(Gf.Vec3f(*pos0))
    joint.CreateLocalRot0Attr(Gf.Quatf(1, 0, 0, 0))
    joint.CreateLocalPos1Attr(Gf.Vec3f(0, 0, 0))
    joint.CreateLocalRot1Attr(Gf.Quatf(1, 0, 0, 0))


def author(stage, root="/Device"):
    """抽屉（平移 0–0.3 m）上带一个摆臂（转动 ±90°），另有未声明行程的平移轴和连续转轴。"""
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    for name in ("", "/base", "/drawer", "/arm", "/free", "/rotor", "/MotionJoints"):
        UsdGeom.Xform.Define(stage, root + name)
    joints = root + "/MotionJoints/"
    _joint(stage, joints + "drawer_joint", "prismatic", root + "/base", root + "/drawer", "X", 0.0, 0.3)
    _joint(stage, joints + "arm_joint", "revolute", root + "/drawer", root + "/arm", "Z", -90.0, 90.0, (0.1, 0, 0))
    _joint(stage, joints + "free_joint", "prismatic", root + "/base", root + "/free", "Y", -math.inf, math.inf)
    _joint(stage, joints + "rotor_joint", "revolute", root + "/base", root + "/rotor", "Z", -math.inf, math.inf)
    # 库位：固定的交接位、随抽屉走的托盘位、随摆臂转的夹持位
    # 直角龙门：X 轴上挂 Z 轴，Z 轴连杆上有一个工具点；台面基准不在原点
    for name in ("/gantry", "/head"):
        UsdGeom.Xform.Define(stage, root + name)
    _joint(stage, joints + "gantry_x", "prismatic", root + "/base", root + "/gantry", "X", -0.5, 0.5)
    _joint(stage, joints + "head_z", "prismatic", root + "/gantry", root + "/head", "Z", -0.3, 0.3)
    UsdGeom.Xform.Define(stage, root + "/head/Frames/tool")   # 参考点放 Frames，不是放物料的库位
    UsdGeom.Xform.Define(stage, root + "/Frames/deck_origin").AddTranslateOp().Set((0.1, 0.0, 0.05))
    for path, position in ((root + "/Sites/handoff", (0.35, 0.0, 0.2)),
                           (root + "/drawer/Sites/tray", (0.05, 0.0, 0.1)),
                           (root + "/arm/Sites/grip", (0.2, 0.0, 0.0))):
        UsdGeom.Xform.Define(stage, path).AddTranslateOp().Set(position)
    return stage


def make_package(tmp_path):
    package = tmp_path / "pkg"
    if (package / "simready_usd" / "unilab.json").is_file():
        return package
    (package / "simready_usd" / "behavior").mkdir(parents=True)
    stage = author(Usd.Stage.CreateNew(str(package / "simready_usd" / "device.usda")), "/Root")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/Root"))
    stage.GetRootLayer().Save()
    (package / "simready_usd" / "behavior" / "drawer.py").write_text(
        "class Drawer:\n"
        "    def open_door(self, joints):\n"
        "        return {'move': {'drawer_joint': joints.upper('drawer_joint')}, 'duration': 2.0,\n"
        "                'state': {'door': 'open'}}\n\n"
        "    def swing(self, joints, angle_deg):\n"
        "        return {'move': {'arm_joint': angle_deg * 3.141592653589793 / 180}, 'duration': 1.0}\n",
        encoding="utf8")
    (package / "simready_usd" / "unilab.json").write_text(json.dumps({
        "entrypoints": {"usd": "simready_usd/device.usda"},
        "behavior_binding": {"kind": "asset_python", "source_root": "simready_usd/behavior",
                             "implementation": "drawer:Drawer", "files": ["drawer.py"]}}), encoding="utf8")
    return package


