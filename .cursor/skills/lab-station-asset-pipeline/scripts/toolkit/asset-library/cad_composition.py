"""Resolve CAD using the same USD instance paths, including mounting overrides."""
import json
from pathlib import Path
import numpy as np
from pxr import Usd,UsdGeom


def from_library(library, package_path, package_digest=None):
    """Load a pinned closure without any domain authoring repository."""
    import native_package as native
    library=Path(library);recipes={};packages={};pins={}
    def load(path,expected):
        root=native.inside(library,path);native.verify(root,expected)
        body=native.read(root/native.ADAPTER);aid=body['asset_id'];actual=native.digest(root/native.MANIFEST)
        if aid in pins:
            assert pins[aid]==actual,('Conflicting CAD revisions',aid)
            return aid
        pins[aid]=actual;packages[aid]=path
        binding=body['cad_binding'];recipe=native.read(native.inside(root,binding['recipe']))
        recipes[aid]=recipe
        for part in recipe['parts']:
            if 'external_native' in part:
                dep=part['external_native'];folder=native.inside(library,dep['package_path']);native.verify(folder,dep['package_digest'])
                part['native']=str(native.inside(folder,dep['path']))
            else:part['native']=str(native.inside(root,part['native']))
            assert Path(part['native']).suffix.lower()=='.sldprt' and Path(part['native']).is_file()
        for instance in recipe['instances']:
            dep=instance['dependency'];assert load(dep['package_path'],dep['package_digest'])==instance['asset_id']
        return aid
    aid=load(package_path,package_digest)
    return resolve(aid,recipes,packages,library)


def resolve(root_id, recipes, packages, library):
    """recipes are loaded CAD bindings; packages map asset IDs to pinned folders."""
    library=Path(library);stages={};caches={}
    def stage(aid):
        if aid not in stages:
            root=library/packages[aid];meta=json.loads((root/'simready_usd/unilab.json').read_text('utf8'))
            stages[aid]=Usd.Stage.Open(str(root/meta['entrypoints']['usd']));caches[aid]=UsdGeom.XformCache()
        return stages[aid]
    final=stage(root_id);final_cache=caches[root_id];parts=[];joints=[]
    def matrix(aid,path):
        prim=stage(aid).GetPrimAtPath(path);assert prim,(aid,path)
        return np.asarray(caches[aid].GetLocalToWorldTransform(prim))
    def walk(aid,selected,target,stack):
        if aid in stack:raise ValueError('CAD dependency cycle: '+str(stack+[aid]))
        recipe=recipes[aid]
        def map_path(path):
            return target+path[len(selected):] if path==selected or path.startswith(selected+'/') else None
        for row in recipe['parts']:
            pairs=[(p,map_path(p)) for p in row['usd_meshes'] if map_path(p)]
            if not pairs:continue
            assert len(pairs)==len(row['usd_meshes']),'A CAD solid cannot be partially sliced by a mesh reference'
            transforms=[]
            for source_path,destination in pairs:
                prim=final.GetPrimAtPath(destination);assert prim,destination
                transforms.append(np.asarray(row['transform'])@np.linalg.inv(matrix(aid,source_path))@
                    np.asarray(final_cache.GetLocalToWorldTransform(prim)))
            assert all(np.max(abs(t-transforms[0]))<2e-6 for t in transforms),'Mount override requires splitting this native solid'
            parts.append(dict(row,transform=transforms[0].tolist(),usd_meshes=[p[1] for p in pairs],owner_asset_id=aid))
        for name,joint in recipe.get('joints',{}).items():
            path=joint['path'];path=path if path.startswith('/') else recipe['root_prim']+'/'+path
            mapped=map_path(path)
            if mapped:
                prim=final.GetPrimAtPath(mapped);assert prim,mapped
                world=np.asarray(final_cache.GetLocalToWorldTransform(prim))
                resolved=dict(joint,path=mapped,origin_m=world[3,:3].tolist())
                if joint.get('parent_path'):resolved['parent_path']=map_path(joint['parent_path'])
                if joint['kind']!='fixed':
                    delta=np.linalg.inv(matrix(aid,path))@world
                    direction=np.eye(3)['XYZ'.index(joint['axis'])]@delta[:3,:3]
                    direction/=np.linalg.norm(direction);axis=int(np.argmax(abs(direction)))
                    assert abs(direction[axis])>1.-1e-5,('Non-cardinal native joint frame requires an explicit frame adapter',mapped,direction)
                    resolved['axis']='XYZ'[axis]
                    if direction[axis]<0 and 'limits' in resolved:resolved['limits']=sorted(-q for q in resolved['limits'])
                joints.append(resolved)
        for instance in recipe['instances']:
            mapped=map_path(instance['prim'])
            if mapped:
                child=recipes[instance['asset_id']]
                walk(instance['asset_id'],instance['source_prim'] or child['root_prim'],mapped,stack+[aid])
    root=recipes[root_id]['root_prim'];walk(root_id,root,root,[])
    groups=[dict(id='base',parent=None,origin_m=[0.,0.,0.],parts=[])]
    moving={}
    for i,joint in enumerate(sorted(joints,key=lambda j:(len(j['path']),j['path']))):
        path=joint['path'];assert path not in moving,path
        parents=[p for p in moving if path.startswith(p+'/')]
        group=dict(id='joint_'+str(i),parent=moving[max(parents,key=len)]['id'] if parents else 'base',
            path=path,origin_m=joint['origin_m'],joint=joint,parts=[])
        moving[path]=group;groups.append(group)
    for path,group in moving.items():
        parent_path=group['joint'].get('parent_path')
        if parent_path:
            assert parent_path in moving,('Missing CAD mounting parent',parent_path)
            group['parent']=moving[parent_path]['id']
    for part in parts:
        paths=[p for p in moving if part['usd_meshes'][0].startswith(p+'/') or part['usd_meshes'][0]==p]
        group=moving[max(paths,key=len)] if paths else groups[0];group['parts'].append(part)
    return dict(name=Path(packages[root_id]).name,groups=groups,asset_id=root_id,
        provenance='Resolved CAD bindings against actual composed USD transforms; no author repository import')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ('library','package','output'):parser.add_argument('--'+field,required=True)
    parser.add_argument('--digest');args=parser.parse_args()
    recipe=from_library(args.library,args.package,args.digest)
    Path(args.output).write_text(json.dumps(recipe,ensure_ascii=False,indent=2),encoding='utf8')
    print('CAD closure',sum(len(g['parts']) for g in recipe['groups']),'occurrences',len(recipe['groups'])-1,'joints')
