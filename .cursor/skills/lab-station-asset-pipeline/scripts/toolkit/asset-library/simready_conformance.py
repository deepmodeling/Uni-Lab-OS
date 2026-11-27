"""Evidence-backed SimReady packaging repairs on unsealed working USD only.

Does not invent mass, licensing, physical material, or engine qualification.
The caller supplies sourced identity/mass; shape/count/normal values are derived.
"""
import argparse, datetime, hashlib, json, math, re
from pathlib import Path

import numpy as np
from PIL import Image
from pxr import Gf, Usd, UsdGeom, UsdPhysics, Vt

INPUT_FIELDS = ('author','asset_name','asset_type','asset_license','category',
                'source_file','usd_date_generated','qcode','mass')
KINDS = {'source','vendor','derived','measured','declared'}
PLACEHOLDERS = {'unknown','none','null','tbd','n/a','todo','unspecified'}

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,data):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')

def validate_contract(metadata, provenance, partial=False):
    if (set(metadata)-set(INPUT_FIELDS)) or (not partial and set(metadata)!=set(INPUT_FIELDS)):
        raise ValueError('Metadata must contain exactly the input fields; geometry/count are derived: '+str(INPUT_FIELDS))
    for field in metadata:
        value=metadata[field]
        if field!='mass' and (not isinstance(value,str) or not value.strip() or value.strip().lower() in PLACEHOLDERS):
            raise ValueError('Unresolved metadata: '+field)
        entry=provenance.get(field,{})
        if entry.get('kind') not in KINDS or not isinstance(entry.get('reference'),str) or not entry['reference'].strip():
            raise ValueError('Missing provenance for '+field)
        if entry.get('kind')=='derived' and not entry.get('method'):
            raise ValueError('Derived metadata needs method: '+field)
    if 'mass' in metadata:
        mass=metadata['mass']
        if isinstance(mass,bool) or not isinstance(mass,(float,int)) or not math.isfinite(mass) or mass<=0:
            raise ValueError('Mass needs finite positive kilograms with evidence; no placeholder')
        if provenance['mass']['kind']=='declared':raise ValueError('Mass cannot be a declaration without source/measurement/calculation')
    if 'asset_license' in metadata and provenance['asset_license']['kind'] not in {'source','vendor'}:
        raise ValueError('License must reference source terms; converter cannot invent a grant')
    if 'usd_date_generated' in metadata:datetime.date.fromisoformat(metadata['usd_date_generated'])
    if 'qcode' in metadata and not re.fullmatch(r'Q[0-9]+',metadata['qcode']):raise ValueError('Invalid Wikidata Q-code')

def derive_missing_normals(stage):
    """Author per-face normals, with no welding, smoothing, or geometry mutation."""
    rows=[]
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):continue
        mesh=UsdGeom.Mesh(prim)
        if mesh.GetSubdivisionSchemeAttr().Get() in ('catmullClark','loop'):continue
        if mesh.GetNormalsAttr().HasAuthoredValue():
            normals=np.asarray(mesh.GetNormalsAttr().Get(),dtype=float)
            if not len(normals) or not np.isfinite(normals).all():raise ValueError('Invalid existing normals '+str(prim.GetPath()))
            continue
        pts=np.asarray(mesh.GetPointsAttr().Get(),dtype=np.float64)
        counts=np.asarray(mesh.GetFaceVertexCountsAttr().Get(),dtype=np.int64)
        inds=np.asarray(mesh.GetFaceVertexIndicesAttr().Get(),dtype=np.int64)
        if not np.isfinite(pts).all() or np.any(counts<3) or counts.sum()!=len(inds) or np.any(inds<0) or np.any(inds>=len(pts)):
            raise ValueError('Cannot derive normals from invalid topology '+str(prim.GetPath()))
        if np.all(counts==3):
            tri=pts[inds.reshape(-1,3)]
            normals=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])
        else:
            normals=[];offset=0
            for count in counts:
                polygon=pts[inds[offset:offset+count]];offset+=count
                normals.append(np.cross(polygon,np.roll(polygon,-1,axis=0)).sum(axis=0))
            normals=np.asarray(normals)
        lengths=np.linalg.norm(normals,axis=1)
        if np.any(lengths<=1e-20):raise ValueError('Degenerate source faces require explicit repair '+str(prim.GetPath()))
        normals/=lengths[:,None]
        if mesh.GetOrientationAttr().Get()==UsdGeom.Tokens.leftHanded:normals=-normals
        mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(normals.astype(np.float32)))
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.uniform)
        prim.SetCustomDataByKey('unilab:normal_provenance','derived_from_persisted_face_winding_flat_normals')
        rows.append({'prim':str(prim.GetPath()),'faces':len(counts),'kind':'derived','interpolation':'uniform',
                     'method':'normalized face cross product/Newell sum; left-handed winding respected; no source geometry change'})
    return rows

def derive_missing_extents(stage):
    """Author bounds for static meshes, including non-rendered collider meshes."""
    rows=[]
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):continue
        mesh=UsdGeom.Mesh(prim)
        if mesh.GetExtentAttr().HasAuthoredValue():continue
        if mesh.GetPointsAttr().GetNumTimeSamples():
            raise ValueError('Animated mesh bounds require explicit time-sampled handling '+str(prim.GetPath()))
        pts=np.asarray(mesh.GetPointsAttr().Get(),dtype=float)
        if pts.ndim!=2 or pts.shape[1]!=3 or not len(pts) or not np.isfinite(pts).all():
            raise ValueError('Cannot derive extents from empty/nonfinite mesh '+str(prim.GetPath()))
        bounds=[Gf.Vec3f(*pts.min(0)),Gf.Vec3f(*pts.max(0))]
        mesh.CreateExtentAttr(bounds)
        rows.append({'prim':str(prim.GetPath()),'kind':'derived','method':'exact componentwise minimum/maximum of persisted static local points',
                     'extent':[list(v) for v in bounds]})
    return rows

def finish(usd_path, metadata, provenance, thumbnail_path, receipt_path=None, *, partial=False, unresolved=None):
    """Finish an unsealed mutable derivative in place; never edits original packages.

    metadata: exactly INPUT_FIELDS by default; partial=True accepts a sourced subset.
    Missing values remain absent in USD, with explicit reasons in the receipt.
    mass is whole-asset kg, independent of body count or available link inertias.
    provenance: field -> {kind, reference, optional sha256/selector/method}.
    thumbnail_path: actual renderer PNG for this derivative/configuration.
    """
    usd_path=Path(usd_path).resolve();thumbnail_path=Path(thumbnail_path).resolve() if thumbnail_path else None
    validate_contract(metadata,provenance,partial=partial)
    missing={field:{'status':'unknown','reason':(unresolved or {}).get(field,'No verifiable value supplied; omitted from USD metadata')} for field in INPUT_FIELDS if field not in metadata}
    repair_failures=[]
    for parent in usd_path.parents:
        if (parent/'bundle.sha256.json').exists():raise ValueError('Refuse editing a sealed package; create a new derivative')
    before=digest(usd_path);stage=Usd.Stage.Open(str(usd_path))
    if not stage or not stage.GetDefaultPrim():raise ValueError('USD has no default prim')
    sidecar=usd_path.with_suffix('.json')
    if sidecar.exists():raise ValueError('Existing metadata sidecar must be reconciled explicitly before finishing')
    def repair_meshes(repair):
        if not partial:return repair(stage)
        rows=[]
        for prim in stage.Traverse():
            if not prim.IsA(UsdGeom.Mesh):continue
            class SingleMesh:
                def Traverse(self):return iter([prim])
            try:rows.extend(repair(SingleMesh()))
            except (ValueError,RuntimeError) as exc:
                repair_failures.append({'prim':str(prim.GetPath()),'operation':repair.__name__,'status':'unresolved','reason':str(exc)})
        return rows
    extent_rows=repair_meshes(derive_missing_extents)
    normal_rows=repair_meshes(derive_missing_normals)
    bounds=UsdGeom.BBoxCache(Usd.TimeCode.Default(),[UsdGeom.Tokens.default_,UsdGeom.Tokens.render],useExtentsHint=False).ComputeWorldBound(stage.GetDefaultPrim()).ComputeAlignedRange()
    size=np.asarray(bounds.GetSize(),dtype=float)*UsdGeom.GetStageMetersPerUnit(stage)
    valid_size=np.isfinite(size).all() and np.all(size>0)
    if not valid_size and not partial:raise ValueError('Asset extents must be finite positive measured geometry dimensions')
    bodies=[str(p.GetPath()) for p in Usd.PrimRange(stage.GetDefaultPrim()) if p.HasAPI(UsdPhysics.RigidBodyAPI)]
    generated=dict(metadata,rigid_body_count=len(bodies))
    if valid_size:generated['asset_extents']=Gf.Vec3d(*size)
    else:missing['asset_extents']={'status':'unknown','reason':'Visible bounds are empty/nonfinite or have a non-positive dimension; no padding invented'}
    # One canonical location prevents SR.004 duplicates across sidecar/root metadata.
    layer=stage.GetRootLayer();data=dict(layer.customLayerData)
    for key in (*INPUT_FIELDS,'rigid_body_count','asset_extents'):data.pop(key,None)
    data['SimReady_Metadata']=generated;layer.customLayerData=data
    thumbnail=None
    if thumbnail_path is None and not partial:raise ValueError('Thumbnail PNG required')
    if thumbnail_path is not None:
      with Image.open(thumbnail_path) as src:
        if src.format!='PNG':raise ValueError('Thumbnail must be actual PNG, not extension relabeling')
        img=src.convert('RGBA');img.thumbnail((256,256),Image.Resampling.LANCZOS)
        thumb=Image.new('RGBA',(256,256),(0,0,0,0));thumb.alpha_composite(img,((256-img.width)//2,(256-img.height)//2))
        dest=usd_path.parent/'.thumbs/256x256'/f'{usd_path.name}.png';dest.parent.mkdir(parents=True,exist_ok=True);thumb.save(dest)
        thumbnail={'path':str(dest),'sha256':digest(dest),'source':str(thumbnail_path),'source_sha256':digest(thumbnail_path),'size':[256,256]}
    else:missing['thumbnail']={'status':'unknown','reason':'No verified preview supplied; no placeholder image generated'}
    layer.Save()
    if valid_size:generated['asset_extents']=size.tolist()
    derived_provenance={'rigid_body_count':{'kind':'derived','method':'count applied RigidBodyAPI under defaultPrim','reference':str(usd_path),'bodies':bodies}}
    if valid_size:derived_provenance['asset_extents']={'kind':'derived','method':'default/render visible world bounds multiplied by metersPerUnit','reference':str(usd_path)}
    receipt={'status':'completed_partial' if missing or repair_failures else 'completed','scope':'metadata, package thumbnail and derived normals; not engine or licensing certification',
             'usd':str(usd_path),'input_sha256':before,'output_sha256':digest(usd_path),'metadata':generated,
             'provenance':dict(provenance,**derived_provenance),
             'derived_normals':normal_rows,'derived_extents':extent_rows,'thumbnail':thumbnail,
             'partial_mode':partial,'unresolved_metadata':missing,'repair_failures':repair_failures,
             'unknown_physics_not_filled':True}
    if receipt_path:write(receipt_path,receipt)
    return receipt

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--usd',required=True);ap.add_argument('--contract',required=True);ap.add_argument('--thumbnail');ap.add_argument('--output',required=True);ap.add_argument('--partial',action='store_true')
    args=ap.parse_args();contract=json.loads(Path(args.contract).read_text(encoding='utf8'))
    result=finish(args.usd,contract['metadata'],contract['provenance'],args.thumbnail,args.output,partial=args.partial,unresolved=contract.get('unresolved'))
    print(json.dumps({'status':result['status'],'derived_normal_meshes':len(result['derived_normals']),'rigid_bodies':result['metadata']['rigid_body_count']}))
