"""Read-only USD structural gate; independent of vendor SimReady profile results."""
import argparse,json,math,sys
import numpy as np
from pathlib import Path
from pxr import Usd,UsdGeom,UsdPhysics,UsdUtils
from core import digest,write

def inspect(path,root=None):
    path=Path(path).resolve();root=Path(root or path.parent).resolve();stage=Usd.Stage.Open(str(path))
    if not stage:raise ValueError('USD 无法重开')
    layers,assets,unresolved=UsdUtils.ComputeAllDependencies(str(path))
    dependencies=sorted({str(Path(l.realPath).resolve()) for l in layers if l.realPath}|{str(Path(a).resolve()) for a in assets})
    outside=[p for p in dependencies if not Path(p).is_relative_to(root)]
    failures=[];warnings=[];meshes=[];physics=[]
    if unresolved:failures.append({'check':'dependencies','unresolved':[str(x) for x in unresolved]})
    if outside:failures.append({'check':'dependencies_outside_package','paths':outside})
    if not stage.GetDefaultPrim():failures.append({'check':'default_prim_missing'})
    units=UsdGeom.GetStageMetersPerUnit(stage);axis=str(UsdGeom.GetStageUpAxis(stage))
    if abs(units-1)>1e-9:failures.append({'check':'meters_per_unit','actual':units})
    # Imported robot libraries commonly place geometry below instanceable references.
    # Count actual occurrences through proxies without de-instancing the source.
    for prim in Usd.PrimRange.Stage(stage,Usd.TraverseInstanceProxies()):
        if prim.IsA(UsdGeom.Mesh):
            mesh=UsdGeom.Mesh(prim);points=mesh.GetPointsAttr().Get() or [];indices=mesh.GetFaceVertexIndicesAttr().Get() or [];counts=mesh.GetFaceVertexCountsAttr().Get() or []
            finite=bool(np.isfinite(np.asarray(points)).all())
            topology=bool(points) and bool(counts) and sum(counts)==len(indices) and all(0<=i<len(points) for i in indices) and all(c>=3 for c in counts)
            item={'prim':str(prim.GetPath()),'points':len(points),'faces':len(counts),'finite':finite,'topology':topology,'normals_authored':mesh.GetNormalsAttr().HasAuthoredValueOpinion()}
            if not finite or not topology:failures.append({'check':'mesh_structure',**item})
            meshes.append(item)
        schemas=list(prim.GetAppliedSchemas())
        if any('Physics' in s or 'Physx' in s for s in schemas) or prim.IsA(UsdPhysics.Joint):
            physics.append({'prim':str(prim.GetPath()),'type':prim.GetTypeName(),'schemas':schemas,'attributes':{a.GetName():str(a.Get()) for a in prim.GetAttributes() if a.GetName().startswith(('physics:','physx'))}})
    if not meshes:failures.append({'check':'no_meshes'})
    bounds=UsdGeom.BBoxCache(Usd.TimeCode.Default(),['default','render','proxy']).ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
    extent=[float(v) for v in bounds.GetSize()] if not bounds.IsEmpty() else []
    if not extent or not all(math.isfinite(v) and v>=0 for v in extent):failures.append({'check':'invalid_bounds'})
    if extent and max(extent)>30:warnings.append('资产跨度超过30m，请核实来源单位或大型场景范围')
    result={'status':'failed' if failures else 'passed','scope':'USD composition, finite geometry, topology indexing, units and dependency closure; not physics execution or NVIDIA certification','usd_sha256':digest(path),'usd_version':'.'.join(map(str,Usd.GetVersion())),'default_prim':str(stage.GetDefaultPrim().GetPath()) if stage.GetDefaultPrim() else None,'meters_per_unit':units,'up_axis':axis,'mesh_count':len(meshes),'faces':sum(m['faces'] for m in meshes),'meshes':meshes,'bounds_size_m':extent,'dependencies':dependencies,'physics_declarations':physics,'failures':failures,'warnings':warnings}
    return result

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('usd');ap.add_argument('--root');ap.add_argument('--output',required=True);a=ap.parse_args();r=inspect(a.usd,a.root);write(a.output,r);print(json.dumps({k:r[k] for k in ('status','mesh_count','faces','bounds_size_m')}));sys.exit(0 if r['status']=='passed' else 1)
