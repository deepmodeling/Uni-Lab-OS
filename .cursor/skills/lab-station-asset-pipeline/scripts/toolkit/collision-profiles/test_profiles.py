import contextlib,hashlib,io,json,tempfile,unittest
from pathlib import Path
import numpy as np
from build_profile import build,rigid_pose

V=np.array([[x,y,z] for x in [0,.02] for y in [0,.02] for z in [0,.02]])
F=np.array([[0,1,3],[0,3,2],[4,6,7],[4,7,5],[0,4,5],[0,5,1],[2,3,7],[2,7,6],[0,2,6],[0,6,4],[1,5,7],[1,7,3]])

class ProfileTests(unittest.TestCase):
 def source(self,root,role='non_CR5_mechanism',pose=None,dense=False):
  arrays={};shapes=[];faces=np.tile(F,(10,1)) if dense else F
  for i in range(2):
   prefix=f's{i}';arrays[prefix+'_vertices']=V+[i*.03,0,0];arrays[prefix+'_counts']=np.full(len(faces),3);arrays[prefix+'_indices']=faces.flatten()
   shapes.append(dict(prim=f'/body/part{i}',type='Mesh',vertices=prefix+'_vertices',face_counts=prefix+'_counts',face_indices=prefix+'_indices',hole_faces=[0] if i==0 else [],point_frame='body_local',source_identity={}))
  p=root/'source.npz';np.savez_compressed(p,**arrays)
  data=dict(status='exported',unsupported=[],arrays=p.name,arrays_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),units='metres',matrix_convention='column_vectors',bodies=[dict(path='/body',role=role,world_from_body_initial=(np.eye(4) if pose is None else pose).tolist(),shapes=shapes)])
  p=root/'source.json';p.write_text(json.dumps(data));return p
 def run_build(self,source,out,**kwargs):
  with contextlib.redirect_stdout(io.StringIO()):return build(source,out,**kwargs)
 def test_exact_tiles_preserve_triangles_and_holes(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);report=self.run_build(self.source(root),root/'out')
   m=json.loads((root/'out/planning-geometry.json').read_text());shapes=m['bodies'][0]['shapes'];self.assertEqual(len(shapes),1)
   with np.load(root/'out/collision.npz') as data:
    sh=shapes[0];points=data[sh['vertices']];faces=data[sh['face_indices']].reshape(-1,3)
    observed=sorted(tuple(sorted(map(tuple,points[f]))) for f in faces)
    expected=sorted(tuple(sorted(map(tuple,(V+[i*.03,0,0])[f]))) for i in range(2) for j,f in enumerate(F) if i!=0 or j!=0)
    self.assertEqual(observed,expected)
   self.assertEqual(report['statistics']['output_triangles'],23)
 def test_pinned_identity_not_merged(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);self.run_build(self.source(root),root/'out',protected=['/body/part0'])
   m=json.loads((root/'out/planning-geometry.json').read_text());self.assertEqual({s['prim'] for s in m['bodies'][0]['shapes']},{'/body/part0','/body/part1'})
 def test_roundoff_is_baked_not_discarded(self):
  m=np.eye(4);m[0,0]+=1e-7;m[:3,3]=[1,2,3];r,c,e=rigid_pose(m)
  np.testing.assert_allclose(r@c,m,atol=1e-14,rtol=0);np.testing.assert_allclose(r[:3,:3].T@r[:3,:3],np.eye(3),atol=1e-14)
  m[0,0]=1.01
  with self.assertRaises(ValueError):rigid_pose(m)
 def test_native_boxes_enclose_all_source_points(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);report=self.run_build(self.source(root,dense=True),root/'out',mode='outer',refinement_outer=True)
   self.assertEqual(report['statistics']['solid_box_shapes'],2)
   m=json.loads((root/'out/planning-geometry.json').read_text())
   for i,shape in enumerate(m['bodies'][0]['shapes']):
    self.assertEqual(shape['type'],'Cube');transform=np.array(shape['body_from_shape'])
    low=transform[:3,3]-np.diag(transform)[:3]/2;high=transform[:3,3]+np.diag(transform)[:3]/2
    self.assertTrue(np.all(V+[i*.03,0,0]>=low));self.assertTrue(np.all(V+[i*.03,0,0]<=high))
   self.assertTrue(report['requires_exact_confirmation'])
 def test_materials_remain_exact(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);report=self.run_build(self.source(root,'material',dense=True),root/'out',mode='outer')
   self.assertEqual(report['statistics']['solid_box_shapes'],0);self.assertEqual(report['statistics']['output_triangles'],239)
 def test_source_digest_mismatch_fails(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p=self.source(root);(root/'source.npz').write_bytes(b'corrupt')
   with self.assertRaisesRegex(ValueError,'digest'):self.run_build(p,root/'out')
if __name__=='__main__':unittest.main()
