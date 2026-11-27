"""按资产几何给没有声明行程的平移关节估一个行程（候选，不是标定值），并给出各连杆的工具点候选。

估法：关节的活动连杆（含它带着的子连杆）沿关节轴方向移动时，包围盒不超出整机包围盒。
    下限 = 整机最小 − 连杆最小，上限 = 整机最大 − 连杆最大（都沿关节轴，关节零位时量）。
工具点候选：连杆包围盒的底面中心（Z 最低处、XY 取中）。
用法：python estimate_limits.py 资产包目录 [--tool 连杆相对路径 ...]   输出 JSON
"""
import argparse, json, math, sys
from pathlib import Path
from pxr import Gf, Usd, UsdGeom, UsdPhysics

parser = argparse.ArgumentParser(); parser.add_argument('package', type=Path); parser.add_argument('--tool', action='append', default=[])
args = parser.parse_args()
meta = json.loads((args.package / 'simready_usd/unilab.json').read_text('utf8'))
stage = Usd.Stage.Open(str(args.package / meta['entrypoints']['usd'])); root = stage.GetDefaultPrim()
cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=False)
xf = UsdGeom.XformCache()
def box(prim):
    r = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    return [list(r.GetMin()), list(r.GetMax())]
whole = box(root)
joints = [p for p in stage.Traverse() if p.IsA(UsdPhysics.PrismaticJoint) or p.IsA(UsdPhysics.RevoluteJoint)]
child_of = {}
for j in joints:
    t = j.GetRelationship('physics:body1').GetTargets(); p0 = j.GetRelationship('physics:body0').GetTargets()
    if len(t) == 1: child_of[str(t[0])] = (j, str(p0[0]) if p0 else None)
def carried(body):
    """这个连杆和它通过关节带着的全部连杆。"""
    out, grew = {body}, True
    while grew:
        grew = False
        for child, (_, parent) in child_of.items():
            if parent in out and child not in out: out.add(child); grew = True
    return out
report = {'package': args.package.name, 'root': str(root.GetPath()), 'whole_bbox_m': whole, 'joints': {}, 'tools': {}}
for j in joints:
    rel = str(j.GetPath())[len(str(root.GetPath())) + 1:]
    lo, hi = j.GetAttribute('physics:lowerLimit').Get(), j.GetAttribute('physics:upperLimit').Get()
    entry = {'type': 'prismatic' if j.IsA(UsdPhysics.PrismaticJoint) else 'revolute', 'declared': [lo, hi]}
    t = j.GetRelationship('physics:body1').GetTargets(); p0 = j.GetRelationship('physics:body0').GetTargets()
    if j.IsA(UsdPhysics.PrismaticJoint) and len(t) == 1:
        parent = xf.GetLocalToWorldTransform(stage.GetPrimAtPath(p0[0])) if p0 else Gf.Matrix4d(1)
        q = j.GetAttribute('physics:localRot0').Get()
        rot = Gf.Matrix4d(1).SetRotate(Gf.Rotation(Gf.Quatd(q.GetReal(), Gf.Vec3d(q.GetImaginary())))) * parent
        axis = rot.TransformDir(Gf.Vec3d(*[1 if a == j.GetAttribute('physics:axis').Get() else 0 for a in 'XYZ'])).GetNormalized()
        mins, maxs = [1e9] * 3, [-1e9] * 3
        for body in carried(str(t[0])):
            b = box(stage.GetPrimAtPath(body))
            mins = [min(mins[i], b[0][i]) for i in range(3)]; maxs = [max(maxs[i], b[1][i]) for i in range(3)]
        k = max(range(3), key=lambda i: abs(axis[i])); sign = 1 if axis[k] > 0 else -1
        entry['axis'] = [round(v, 6) for v in axis]
        if abs(abs(axis[k]) - 1) < 1e-6:
            a, b = whole[0][k] - mins[k], whole[1][k] - maxs[k]
            est = sorted([a * sign, b * sign])
            entry['estimated'] = [round(est[0], 4), round(est[1], 4)]
            entry['link_extent_along_axis_m'] = [round(mins[k], 4), round(maxs[k], 4)]
        entry['unbounded'] = not (lo is not None and hi is not None and math.isfinite(lo) and math.isfinite(hi))
    report['joints'][rel] = entry
for link in args.tool:
    prim = stage.GetPrimAtPath(root.GetPath().AppendPath(link)); b = box(prim)
    world = Gf.Vec3d((b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2, b[0][2])
    local = xf.GetLocalToWorldTransform(prim).GetInverse().Transform(world)
    report['tools'][link] = {'bottom_center_in_link_m': [round(v, 5) for v in local], 'bottom_center_in_asset_m': [round(v, 5) for v in root and xf.GetLocalToWorldTransform(root).GetInverse().Transform(world)], 'link_bbox_m': b}
print(json.dumps(report, ensure_ascii=False, indent=1))
