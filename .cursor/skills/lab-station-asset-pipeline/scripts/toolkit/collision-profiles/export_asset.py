"""Export an asset-local collision input without changing the sealed USD.

Use authored enabled colliders when present. Otherwise use render meshes with
explicitly nonphysical optical/cable geometry excluded. Moving rigid bodies
remain separate. Output uses the existing CadScene contract, not a new runtime.
"""
import argparse,hashlib,json
from pathlib import Path
import numpy as np
from pxr import Usd,UsdGeom,UsdPhysics,Gf


def export(package,output):
 root=Path(package);out=Path(output);out.mkdir(parents=True,exist_ok=False)
 meta=json.loads((root/'simready_usd/unilab.json').read_bytes());usd=root/meta['entrypoints']['usd'];stage=Usd.Stage.Open(str(usd))
 if not stage or not stage.GetDefaultPrim():raise ValueError('Asset default prim required')
 if UsdGeom.GetStageMetersPerUnit(stage)!=1:raise ValueError('Metre normalized asset required')
 cache=UsdGeom.XformCache();base=stage.GetDefaultPrim();root_world=cache.GetLocalToWorldTransform(base);root_inverse=root_world.GetInverse()
 candidates=[];excluded=[]
 for prim in Usd.PrimRange.Stage(stage,Usd.TraverseInstanceProxies()):
  if not prim.IsA(UsdGeom.Gprim):continue
  path=str(prim.GetPath());role=prim.GetAttribute('lab:collisionRole').Get()
  if prim.GetAttribute('physics:collisionEnabled').Get() is False or role in {'optical_path_visual_candidate','optical_ray_visual','flexible_cable_visual'}:
   excluded.append(dict(prim=path,reason='authored_disabled_or_nonphysical'));continue
  candidates.append(prim)
 explicit=[p for p in candidates if p.HasAPI(UsdPhysics.CollisionAPI)]
 selected=explicit or candidates
 if not selected:raise ValueError('No asset geometry')
 arrays={};bodies={};maximum=0.
 for n,prim in enumerate(selected):
  body=prim
  while body and body!=base and not body.HasAPI(UsdPhysics.RigidBodyAPI):body=body.GetParent()
  moving=bool(body and body.HasAPI(UsdPhysics.RigidBodyAPI) and body.GetAttribute('physics:rigidBodyEnabled').Get() is not False)
  bp=str(body.GetPath()) if moving else '/__static_world__'
  body_world=cache.GetLocalToWorldTransform(body)*root_inverse if moving else Gf.Matrix4d(1)
  rigid=Gf.Matrix4d().SetRotate(body_world.ExtractRotationQuat());rigid.SetTranslateOnly(body_world.ExtractTranslation())
  shape_world=cache.GetLocalToWorldTransform(prim)*root_inverse;transform=shape_world*rigid.GetInverse()
  role='material' if meta['reuse_kind'] in {'consumable','workpiece','material','workpiece_consumable'} else ('non_CR5_mechanism' if moving else 'static_world')
  record=bodies.setdefault(bp,dict(path=bp,role=role,world_from_body_initial=np.asarray(rigid).T.tolist(),shapes=[],source_identity={'asset_id':meta['asset_id']}))
  if not prim.IsA(UsdGeom.Mesh):
   if prim.GetTypeName() not in {'Cube','Cylinder','Sphere','Capsule'}:raise ValueError('Unsupported asset primitive: '+str(prim.GetPath()))
   parameters={name:prim.GetAttribute(name).Get() for name in ('size','radius','height','axis') if prim.GetAttribute(name) and prim.GetAttribute(name).HasValue()}
   record['shapes'].append(dict(prim=str(prim.GetPath()),type=prim.GetTypeName(),point_frame='primitive_shape_local_with_body_from_shape',body_from_shape=np.asarray(transform).T.tolist(),parameters=parameters,collision_enabled=True))
   continue
  mesh=UsdGeom.Mesh(prim);v=np.asarray(mesh.GetPointsAttr().Get(),dtype=float);counts=np.asarray(mesh.GetFaceVertexCountsAttr().Get(),dtype=np.int32);indices=np.asarray(mesh.GetFaceVertexIndicesAttr().Get(),dtype=np.int32);holes=list(mesh.GetHoleIndicesAttr().Get() or [])
  if not len(v) or not np.isfinite(v).all() or sum(counts)!=len(indices) or any(c!=3 for i,c in enumerate(counts) if i not in holes):raise ValueError('Asset mesh needs exact tessellation')
  def apply(v,m):m=np.asarray(m);return v@m[:3,:3]+m[3,:3]
  local=apply(v,transform);error=float(np.max(abs(apply(local,rigid)-apply(v,shape_world))));maximum=max(maximum,error)
  prefix='s'+str(n);arrays[prefix+'v']=local;arrays[prefix+'c']=counts;arrays[prefix+'i']=indices
  identity={a.GetName():a.Get() for a in prim.GetAttributes() if a.GetName().startswith('lab:') and isinstance(a.Get(),(str,int,float,bool))}
  role='material' if meta['reuse_kind'] in {'consumable','workpiece','material','workpiece_consumable'} else ('non_CR5_mechanism' if moving else 'static_world')
  record=bodies.setdefault(bp,dict(path=bp,role=role,world_from_body_initial=np.asarray(rigid).T.tolist(),shapes=[],source_identity={'asset_id':meta['asset_id']}))
  record['shapes'].append(dict(prim=str(prim.GetPath()),type='Mesh',point_frame='body_local',vertices=prefix+'v',face_counts=prefix+'c',face_indices=prefix+'i',hole_faces=holes,points=len(v),faces=len(counts),orientation=mesh.GetOrientationAttr().Get(),collision_enabled=True,source_identity=identity))
 np.savez_compressed(out/'collision.npz',**arrays)
 report=dict(schema='postproc.planning-geometry/v1',status='exported',units='metres',up_axis=str(UsdGeom.GetStageUpAxis(stage)),matrix_convention='column_vectors',arrays='collision.npz',arrays_sha256=hashlib.sha256((out/'collision.npz').read_bytes()).hexdigest(),bodies=list(bodies.values()),shape_count=len(selected),excluded=excluded,unsupported=[],geometry_authority='Asset-local authored collision geometry' if explicit else 'Asset-local CAD meshes; nonphysical authored roles excluded',world_roundtrip_max_error_m=maximum,asset_id=meta['asset_id'],asset_default_prim=str(base.GetPath()),asset_root_transform_removed=np.asarray(root_world).T.tolist(),source_package_sha256=hashlib.sha256((root/'com.nvidia.simready.packaging.json').read_bytes()).hexdigest(),source_usd_sha256=hashlib.sha256(usd.read_bytes()).hexdigest(),qualification='geometry_only; task_and_contact_not_run',selection='authored_colliders' if explicit else 'CAD_meshes',hardware=False)
 (out/'planning-geometry.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return report

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',required=True);p.add_argument('--output',required=True);a=p.parse_args();print(json.dumps(export(a.package,a.output),ensure_ascii=False)[:500])
