"""Compare every retained triangle and primitive to the pinned original source."""
import argparse,json,zipfile
from pathlib import Path
import numpy as np
from build_profile import digest,read_array,rigid_pose


def verify(source,exact):
 source,exact=Path(source),Path(exact);a=json.loads(source.read_bytes());b=json.loads(exact.read_bytes())
 if b['source_geometry_sha256']!=digest(source):raise ValueError('Exact profile source differs')
 for p,d in [(source,a),(exact,b)]:
  if digest(p.parent/d['arrays'])!=d['arrays_sha256']:raise ValueError('Arrays digest differs')
 sources={r['path']:r for r in a['bodies']};seen=set();total=0;primitives=0;maximum=0.
 if set(sources)!={r['path'] for r in b['bodies']}:raise ValueError('Rigid body coverage differs')
 with zipfile.ZipFile(source.parent/a['arrays']) as az,zipfile.ZipFile(exact.parent/b['arrays']) as bz:
  for body in b['bodies']:
   old=sources[body['path']];pose,correction,_=rigid_pose(old['world_from_body_initial'])
   if not np.allclose(body['world_from_body_initial'],pose,atol=1e-14,rtol=0):raise ValueError('Body frame differs')
   shapes={s['prim']:s for s in old['shapes']}
   for shape in body['shapes']:
    members=shape.get('source_members',[shape['prim']])
    if shape['type']!='Mesh':
     src=shapes[shape['prim']]
     if src['type']!=shape['type'] or src['parameters']!=shape['parameters'] or not np.allclose(correction@src['body_from_shape'],shape['body_from_shape'],atol=1e-14,rtol=0):raise ValueError('Primitive changed')
     primitives+=1
    else:
     v=read_array(bz,shape['vertices']);indices=read_array(bz,shape['face_indices']).reshape(-1,3);offset=0
     if shape.get('hole_faces'):raise ValueError('Unexpected output holes')
     for name in members:
      src=shapes[name];vertices=read_array(az,src['vertices']);faces=read_array(az,src['face_indices']).reshape(-1,3)
      holes=src.get('hole_faces',[])
      if holes:
       mask=np.ones(len(faces),bool);mask[holes]=False;faces=faces[mask]
      vertices=vertices@correction[:3,:3].T+correction[:3,3]
      actual=v[indices[offset:offset+len(faces)]];expected=vertices[faces]
      if actual.shape!=expected.shape:raise ValueError('Triangle coverage differs')
      error=float(np.max(abs(actual-expected)));maximum=max(maximum,error)
      if error>1e-14:raise ValueError('Exact surface coordinates changed')
      offset+=len(faces);total+=len(faces)
     if offset!=len(indices):raise ValueError('Extra exact triangles')
    for name in members:
     key=(body['path'],name)
     if key in seen:raise ValueError('Source shape used twice')
     seen.add(key)
  wanted={(body['path'],s['prim']) for body in a['bodies'] for s in body['shapes']}
  if seen!=wanted:raise ValueError('Missing source shape')
 return dict(status='passed',source_geometry_sha256=digest(source),exact_geometry_sha256=digest(exact),source_shapes=len(seen),triangles_compared=total,primitives_compared=primitives,maximum_local_coordinate_error_m=maximum,proof='every source triangle in source member order; no added or missing triangles; holes preserved; primitives unchanged')

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--exact',required=True);p.add_argument('--output',required=True);a=p.parse_args();r=verify(a.source,a.exact);Path(a.output).write_text(json.dumps(r,indent=2));print(json.dumps(r))
