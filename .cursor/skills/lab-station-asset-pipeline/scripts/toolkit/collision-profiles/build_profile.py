"""Build conservative, body-local collision profiles without changing display CAD.

Spatial tiles never combine different rigid bodies. Sensitive/contact identities
remain separate. A source mesh is either retained exactly or enclosed by a verified
native solid box; profiles report the deliberately loose diameter error bound. They are
candidate rejection models, not a clearance or contact qualification.
"""
from __future__ import annotations
import argparse, collections, copy, hashlib, json, math, re, zipfile
from pathlib import Path
import numpy as np


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_array(archive, key):
    with archive.open(key + '.npy') as f:
        return np.lib.format.read_array(f, allow_pickle=False)


def rigid_pose(value):
    m=np.asarray(value,dtype=float)
    if m.shape!=(4,4) or not np.isfinite(m).all() or not np.allclose(m[3],[0,0,0,1],atol=1e-12,rtol=0):
        raise ValueError('Invalid body pose')
    r=m[:3,:3];error=float(np.max(abs(r.T@r-np.eye(3))))
    if error>1e-6 or np.linalg.det(r)<=0:raise ValueError('Non-rigid source body pose')
    u,_,vt=np.linalg.svd(r);out=m.copy();out[:3,:3]=u@vt
    return out,np.linalg.inv(out)@m,error


def build(source, output, *, mode='exact', tile_m=.15, hull_extent_m=.25, protected=(), refinement_outer=False):
    if mode not in {'raw','exact','outer'} or not np.isfinite([tile_m,hull_extent_m]).all() or min(tile_m,hull_extent_m)<=0:
        raise ValueError('Invalid collision profile policy')
    if isinstance(protected,(str,bytes)) or any(not isinstance(p,str) or not p for p in protected):raise ValueError('Protected ids must be explicit strings')
    if refinement_outer and mode!='outer':raise ValueError('Refinement screening requires outer mode')
    source=Path(source);output=Path(output)
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    data=json.loads(source.read_bytes());arrays=source.parent/data['arrays']
    if digest(arrays)!=data['arrays_sha256']:raise ValueError('Source arrays digest mismatch')
    if data.get('unsupported') or data.get('status')!='exported':raise ValueError('Incomplete source export')
    protected=set(protected)
    available={b['path'] for b in data['bodies']}|{s['prim'] for b in data['bodies'] for s in b['shapes']}
    if protected-available:raise ValueError('Unknown protected collision identities: '+str(sorted(protected-available)))
    archive_data={};new_bodies=[];records=[];counter=collections.Counter(source_shapes=0,source_triangles=0,output_triangles=0,convex_outer_shapes=0,solid_box_shapes=0,exact_shapes=0,retained_primitives=0);shape_count=0
    with zipfile.ZipFile(arrays) as archive:
        for body in data['bodies']:
            pose,correction,pose_error=rigid_pose(body['world_from_body_initial'])
            new_body=copy.deepcopy(body);new_body['world_from_body_initial']=pose.tolist();new_body['shapes']=[]
            groups={}
            for shape in body['shapes']:
                name=shape['prim'];counter['source_shapes']+=1
                if shape['type']!='Mesh':
                    converted=copy.deepcopy(shape)
                    converted['body_from_shape']=(correction@np.asarray(shape['body_from_shape'])).tolist()
                    new_body['shapes'].append(converted);counter['retained_primitives']+=1;continue
                vertices=read_array(archive,shape['vertices']);counts=read_array(archive,shape['face_counts']);indices=read_array(archive,shape['face_indices'])
                if np.any(counts!=3) or len(indices)!=3*len(counts):raise ValueError('Invalid triangular source')
                faces=indices.reshape(-1,3)
                holes=shape.get('hole_faces',[])
                if holes:
                    mask=np.ones(len(faces),bool);mask[holes]=False;faces=faces[mask]
                if not len(vertices) or not len(faces) or not np.isfinite(vertices).all():raise ValueError('Empty/nonfinite source mesh')
                if faces.min()<0 or faces.max()>=len(vertices):raise ValueError('Invalid vertex index')
                vertices=vertices@correction[:3,:3].T+correction[:3,3]
                counter['source_triangles']+=len(faces)
                # Transported materials and finger mating surfaces retain their exact openings.
                critical=body['role'] in ('material','tool_base','tool_finger') or name in protected or body['path'] in protected
                certificate=None
                source_name=shape.get('source_identity',{}).get('lab:sourceName','')
                dimensions=np.ptp(vertices,axis=0)
                protected_opening=bool(re.search(r'孔|板|架|座|盒|夹|指|框|槽',source_name))
                if (mode=='outer' and not critical and (refinement_outer or (not holes and not protected_opening and dimensions.max()<=hull_extent_m))
                    and len(faces)>=80 and dimensions.min()>1e-9):
                    center=(vertices.min(0)+vertices.max(0))*.5
                    transform=np.diag([*(dimensions+2e-9),1.]);transform[:3,3]=center
                    native=dict(prim=name,type='Cube',parameters=dict(size=1.),body_from_shape=transform.tolist(),source_members=[name],collision_enabled=True)
                    group=shape.get('source_identity',{}).get('lab:group') or ('/'.join(name.split('/')[:3]) if body['role']=='static_world' else body['path'])
                    tile=tuple(np.floor(center/tile_m).astype(int))
                    native['collision_object_id']=(body['path']+'/CollisionTiles/o_'+hashlib.sha256(str((group,tile)).encode()).hexdigest()[:20] if body['role']=='static_world' else body['path'])
                    new_body['shapes'].append(native);counter['solid_box_shapes']+=1
                    records.append(dict(body=body['path'],prim=name,source_members=[name],triangles=0,pose_roundoff_orthogonality_error=pose_error,
                        approximations=[dict(source=name,kind='native_solid_box',padding_m=1e-9,maximum_added_distance_bound_m=float(np.linalg.norm(dimensions+2e-9)),source_triangles=len(faces))]))
                    continue
                counter['convex_outer_shapes' if certificate else 'exact_shapes']+=1
                counter['output_triangles']+=len(faces)
                group=shape.get('source_identity',{}).get('lab:group') or ('/'.join(name.split('/')[:3]) if body['role']=='static_world' else body['path'])
                tile=tuple(np.floor((vertices.min(0)+vertices.max(0))*.5/tile_m).astype(int))
                # Keep explicit contact object identities; spatial grouping affects only unprotected shapes.
                key=('pinned',name) if name in protected else (group,tile)
                if mode=='raw':key=('source',name)
                groups.setdefault(key,[]).append((shape,vertices,faces,certificate))
            for key,members in groups.items():
                vertices=[];triangles=[];offset=0
                for _,v,f,_ in members:vertices.append(v);triangles.append(f+offset);offset+=len(v)
                v=np.concatenate(vertices);f=np.concatenate(triangles)
                # Deduplicate vertices exactly; retain all triangles and original holes.
                if mode!='raw':
                    unique,inverse=np.unique(v,axis=0,return_inverse=True);mapped=inverse[f]
                    if np.all((mapped[:,0]!=mapped[:,1])&(mapped[:,1]!=mapped[:,2])&(mapped[:,0]!=mapped[:,2])):v,f=unique,mapped
                    else:counter['groups_preserved_for_degenerate_source']+=1
                prefix='s'+str(shape_count);shape_count+=1
                names=[x[0]['prim'] for x in members]
                if len(members)==1:identity=names[0]
                else:identity=body['path']+'/CollisionTiles/t_'+hashlib.sha256('\n'.join(names).encode()).hexdigest()[:20]
                archive_data[prefix+'_vertices']=v.astype('<f8');archive_data[prefix+'_counts']=np.full(len(f),3,dtype='<i4');archive_data[prefix+'_indices']=f.reshape(-1).astype('<i4')
                entry=dict(prim=identity,type='Mesh',vertices=prefix+'_vertices',face_counts=prefix+'_counts',face_indices=prefix+'_indices',point_frame='body_local',hole_faces=[],points=len(v),faces=len(f),body_from_shape=np.eye(4).tolist(),collision_enabled=True,approximation='none',source_members=names)
                if names[0] in protected and len(names)==1:entry['collision_object_id']=names[0]
                elif body['role']=='static_world':entry['collision_object_id']=(identity if mode=='raw' else body['path']+'/CollisionTiles/o_'+hashlib.sha256(str(key).encode()).hexdigest()[:20])
                else:entry['collision_object_id']=body['path']
                new_body['shapes'].append(entry)
                records.append(dict(body=body['path'],prim=identity,source_members=names,triangles=len(f),pose_roundoff_orthogonality_error=pose_error,approximations=[dict(source=x[0]['prim'],**x[3]) for x in members if x[3]]))
            new_bodies.append(new_body)
            print(body['path'],len(body['shapes']),'->',len(new_body['shapes']),flush=True)
    array_path=output/'collision.npz'
    np.savez_compressed(array_path,**archive_data)
    metadata=dict(data);metadata.update(bodies=new_bodies,arrays=array_path.name,arrays_sha256=digest(array_path),shape_count=sum(len(b['shapes']) for b in new_bodies),source_geometry_sha256=digest(source),geometry_authority='Exact tiled surfaces' if mode!='outer' else 'Native solid box outer profiles; critical interfaces exact',collision_profile='profile.json')
    (output/'planning-geometry.json').write_text(json.dumps(metadata,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    report=dict(schema='unilab.collision-profile/v1',status='built_not_trajectory_qualified',mode=mode,requires_exact_confirmation=bool(mode=='outer'),refinement_outer=refinement_outer,producer_sha256=digest(__file__),source_geometry_sha256=digest(source),geometry_sha256=digest(output/'planning-geometry.json'),tile_m=tile_m,hull_extent_m=hull_extent_m,protected_ids=sorted(protected),statistics=dict(counter),bodies=records,limits=['Discrete trajectory checking only','Outer-profile collisions require exact-source confirmation','No contact/torque or continuous-collision qualification'])
    (output/'profile.json').write_text(json.dumps(report,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--mode',choices=['raw','exact','outer'],default='exact');p.add_argument('--protected-json');p.add_argument('--refinement-outer',action='store_true');a=p.parse_args()
    protected=json.loads(Path(a.protected_json).read_bytes()) if a.protected_json else []
    report=build(a.source,a.output,mode=a.mode,protected=protected,refinement_outer=a.refinement_outer);print(json.dumps(report['statistics']))

if __name__=='__main__':main()
