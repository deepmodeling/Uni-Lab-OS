"""CAD tessellation to self-contained USD; preserves hierarchy and stable IDs."""
import sys,argparse
sys.dont_write_bytecode=True
from pathlib import Path
from core import *
from pxr import Usd,UsdGeom,Gf,Vt,Sdf,UsdShade

def derive(root):
    root=Path(root).resolve();p=read(root/'package.json');defs=read(root/p['definitions'])['definitions'];rows=read(root/p['assembly'])['occurrences'];byid={r['id']:r for r in rows}
    target=root/'representations/usd/asset.usdc';target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():raise ValueError('不可覆盖既有表示')
    stage=Usd.Stage.CreateNew(str(target));UsdGeom.SetStageMetersPerUnit(stage,1.);UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.z)
    top=UsdGeom.Xform.Define(stage,'/Asset');stage.SetDefaultPrim(top.GetPrim());paths={}
    def prim_path(row):
        if row['id'] not in paths:paths[row['id']]=('/Asset' if row['parent'] is None else prim_path(byid[row['parent']]))+'/o_'+row['id'].replace('-','_')
        return paths[row['id']]
    for r in rows:
        d=defs[r['definition_id']];path=prim_path(r);xf=UsdGeom.Xform.Define(stage,path)
        if d.get('calculated_properties',{}).get('values'):
            xf.GetPrim().CreateAttribute('unilab:cad:sourceProperties',Sdf.ValueTypeNames.String).Set(__import__('json').dumps(d['calculated_properties']['values']))
            xf.GetPrim().CreateAttribute('unilab:cad:propertyStatus',Sdf.ValueTypeNames.Token).Set('unverified_material_not_runtime_physics')
        xf.AddTransformOp().Set(Gf.Matrix4d(*[r['local_matrix'][j][i] for i in range(4) for j in range(4)]))
        for k in ('id','definition_id','definition_revision','configuration'):xf.GetPrim().SetCustomDataByKey('engineering:'+k,r[k])
        if d['kind']=='part':
            xyz=read(root/d['mesh']['path'])['triangles_xyz_m'];mesh=UsdGeom.Mesh.Define(stage,path+'/Geometry')
            mesh.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*xyz[i:i+3]) for i in range(0,len(xyz),3)]))
            mesh.CreateFaceVertexCountsAttr([3]*(len(xyz)//9));mesh.CreateFaceVertexIndicesAttr(list(range(len(xyz)//3)));mesh.CreateSubdivisionSchemeAttr('none')
            rgb=d.get('appearance',{}).get('rgb')
            if rgb and all(0<=x<=1 for x in rgb):
                mat=UsdShade.Material.Define(stage,path+'/Material');shader=UsdShade.Shader.Define(stage,path+'/Material/Preview')
                shader.CreateIdAttr('UsdPreviewSurface');shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb));mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(),'surface');UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mat)
    stage.GetRootLayer().Save();stage=None
    reopened=Usd.Stage.Open(str(target));cache=UsdGeom.XformCache();maximum=0.;scalars=0;triangles=0
    for r in rows:
        prim=reopened.GetPrimAtPath(paths[r['id']]);actual=cache.GetLocalToWorldTransform(prim)
        maximum=max(maximum,error([[actual[j][i] for j in range(4)] for i in range(4)],r['world_matrix']))
        for k in ('id','definition_id','definition_revision','configuration'):
            if prim.GetCustomDataByKey('engineering:'+k)!=r[k]:raise ValueError('USD 身份变化')
        d=defs[r['definition_id']]
        if d['kind']=='part':
            mesh=UsdGeom.Mesh(reopened.GetPrimAtPath(paths[r['id']]+'/Geometry'));xyz=read(root/d['mesh']['path'])['triangles_xyz_m'];actual=[float(x) for v in mesh.GetPointsAttr().Get() for x in v]
            if len(actual)!=len(xyz) or list(mesh.GetFaceVertexIndicesAttr().Get())!=list(range(len(xyz)//3)):raise ValueError('USD 网格变化')
            if list(mesh.GetFaceVertexCountsAttr().Get())!=[3]*(len(xyz)//9):raise ValueError('USD 面变化')
            maximum=max(maximum,max(abs(x-y) for x,y in zip(actual,xyz)));scalars+=len(xyz);triangles+=len(xyz)//9
    if maximum>2e-7:raise ValueError('USD 位姿/顶点超限')
    write(root/'evidence/usd-reopen.json',{'status':'passed','version':'.'.join(map(str,Usd.GetVersion())),'max_error_m':maximum,'all_vertex_scalars':scalars,'triangles':triangles,'occurrences':len(rows),'metersPerUnit':UsdGeom.GetStageMetersPerUnit(reopened),'upAxis':str(UsdGeom.GetStageUpAxis(reopened)),'physics_qualified':False})
    p['entrypoints']['usd']=target.relative_to(root).as_posix();p['representations'].append({'kind':'usd','path':p['entrypoints']['usd'],'sha256':digest(target)})
    p['capabilities']['usd_reopen']={'status':'passed','evidence':'evidence/usd-reopen.json'}
    if p['schema']=='unilab.engineering-asset/3':
        proof=read(root/'evidence/usd-reopen.json')
        proof['input_files']={path:digest(root/path) for path in (p['entrypoints']['usd'],p['definitions'],p['assembly'])}
        proof['new_engine_execution']=True;write(root/'evidence/usd-reopen.json',proof)
    write(root/'package.json',p)
    return seal(root)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--package',required=True);a=ap.parse_args();print(derive(a.package))
