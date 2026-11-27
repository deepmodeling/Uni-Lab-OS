"""在暂存区生成候选资产修订：引用已发布包的 USD，换上按 OS @action 规范写的行为类。

    python make_candidate.py 已发布包目录 输出目录 行为文件.py 类名 [--limit 关节相对路径=上限度数:依据]

不修改已发布的封存包，不入库、不发布。可选的 --limit 只覆盖转动关节上限（度），并把原值和依据写进
unilab.json 的 candidate.changes。
"""
import argparse, json, shutil
from pathlib import Path
from pxr import Sdf, Usd, UsdGeom

parser = argparse.ArgumentParser()
parser.add_argument('source', type=Path); parser.add_argument('output', type=Path)
parser.add_argument('behavior', type=Path); parser.add_argument('class_name')
parser.add_argument('--limit', action='append', default=[])
parser.add_argument('--range', action='append', default=[], help='平移关节行程：关节相对路径=下限,上限（米）:依据')
parser.add_argument('--site', action='append', default=[], help='新增库位：相对根的路径=x,y,z:依据（路径里要含 Sites 一级）')
args = parser.parse_args()
src, out = args.source.resolve(), args.output.resolve()
meta = json.loads((src / 'simready_usd/unilab.json').read_text('utf8'))
original = src / meta['entrypoints']['usd']
stage = Usd.Stage.Open(str(original)); root = stage.GetDefaultPrim()
shutil.rmtree(out, ignore_errors=True); (out / 'simready_usd/behavior').mkdir(parents=True)
layer = Sdf.Layer.CreateNew(str(out / 'simready_usd/candidate.usda'))
new = Usd.Stage.Open(layer)
UsdGeom.SetStageMetersPerUnit(new, UsdGeom.GetStageMetersPerUnit(stage)); UsdGeom.SetStageUpAxis(new, UsdGeom.GetStageUpAxis(stage))
top = new.DefinePrim('/' + root.GetName(), root.GetTypeName()); top.GetReferences().AddReference(str(original)); new.SetDefaultPrim(top)
changes = []
for item in args.limit:
    target, _, evidence = item.partition(':'); joint, _, value = target.partition('=')
    prim = stage.GetPrimAtPath(root.GetPath().AppendPath(joint))
    if not prim: raise SystemExit('关节不存在：' + joint)
    new.OverridePrim(top.GetPath().AppendPath(joint)).CreateAttribute('physics:upperLimit', Sdf.ValueTypeNames.Float).Set(float(value))
    changes.append({'joint': joint, 'field': 'physics:upperLimit', 'from_deg': prim.GetAttribute('physics:upperLimit').Get(),
                    'to_deg': float(value), 'evidence': evidence})
for item in args.range:
    target, _, evidence = item.partition(':'); joint, _, bounds = target.partition('=')
    prim = stage.GetPrimAtPath(root.GetPath().AppendPath(joint))
    if not prim: raise SystemExit('关节不存在：' + joint)
    lower, upper = (float(v) for v in bounds.split(','))
    over = new.OverridePrim(top.GetPath().AppendPath(joint))
    over.CreateAttribute('physics:lowerLimit', Sdf.ValueTypeNames.Float).Set(lower)
    over.CreateAttribute('physics:upperLimit', Sdf.ValueTypeNames.Float).Set(upper)
    changes.append({'joint': joint, 'field': 'physics:lowerLimit/upperLimit', 'from': [str(prim.GetAttribute('physics:lowerLimit').Get()), str(prim.GetAttribute('physics:upperLimit').Get())],
                    'to_m': [lower, upper], 'evidence': evidence})
for item in args.site:
    target, _, evidence = item.partition(':'); path, _, xyz = target.partition('=')
    prim = UsdGeom.Xform.Define(new, top.GetPath().AppendPath(path))
    prim.AddTranslateOp().Set(tuple(float(v) for v in xyz.split(',')))
    changes.append({'site': path, 'position_m': [float(v) for v in xyz.split(',')], 'evidence': evidence})
layer.Save()
shutil.copy(args.behavior, out / 'simready_usd/behavior/behavior.py')
meta = dict(meta, entrypoints={'usd': 'simready_usd/candidate.usda'},
            behavior_binding={'kind': 'asset_python', 'source_root': 'simready_usd/behavior',
                              'implementation': 'behavior:' + args.class_name, 'files': ['behavior.py'],
                              'requires': [],
                              'qualification': 'explicit-clock geometry kinematics; no physics/hardware qualification'},
            candidate={'status': 'staging_not_published', 'based_on': {'asset_id': meta['asset_id'], 'package': src.name},
                       'changes': changes})
(out / 'simready_usd/unilab.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), 'utf8')
print('candidate ->', out)
