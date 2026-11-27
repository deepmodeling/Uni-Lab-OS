"""无求解器 USD 场景；供 Isaac K 使用，SDK 生命周期由 OS worker 拥有。"""
import math
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics


class Scene:
    """读回 USD 实际变换；附着通过命名空间重挂，保留物体世界姿态。"""
    def __init__(self, stage):
        self.stage = stage
        self.objects = {}
        UsdGeom.SetStageMetersPerUnit(stage, 1.)
        UsdGeom.SetStageUpAxis(stage, 'Z')
        UsdGeom.Xform.Define(stage, '/Kinematic')
        self.update()

    def _prim(self, name):
        prim = self.stage.GetPrimAtPath(self.objects[name])
        if not prim:
            raise ValueError('kinematic_object_missing: ' + name)
        return prim

    def create(self, name, matrix, size=None):
        if name in self.objects:
            raise ValueError('kinematic_duplicate_object')
        path = '/Kinematic/object_' + str(len(self.objects))
        prim = UsdGeom.Xform.Define(self.stage, path).GetPrim()
        prim.CreateAttribute('kinematic:name', Sdf.ValueTypeNames.String).Set(name)
        self.objects[name] = path
        self.set_pose(name, matrix)
        if size is not None:
            # 网格尺寸不写入坐标变换，避免改变后续附着子对象的尺度。
            vertices = [(x*size[0]/2,y*size[1]/2,z*size[2]/2)
                        for x,y,z in [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                                      (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]]
            self._mesh(path+'/visual', vertices, [(0,3,2,1),(4,5,6,7),(0,1,5,4),
                                                  (1,2,6,5),(2,3,7,6),(3,0,4,7)])
        return prim

    def _mesh(self, path, vertices, faces):
        mesh = UsdGeom.Mesh.Define(self.stage, path)
        mesh.CreatePointsAttr(vertices)
        mesh.CreateFaceVertexCountsAttr([len(f) for f in faces])
        mesh.CreateFaceVertexIndicesAttr([int(i) for f in faces for i in f])
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        return mesh

    def import_mesh(self, path, name, parent, local_matrix):
        if Path(path).suffix.lower() == '.stl':
            from .scene_robot_model import _read_stl
            vertices, faces = _read_stl(Path(path).read_bytes())
        elif Path(path).suffix.lower() == '.obj':
            vertices, faces = [], []
            for line in Path(path).read_text(encoding='utf-8').splitlines():
                fields = line.split()
                if not fields or fields[0].startswith('#'):
                    continue
                if fields[0] == 'v':
                    vertices.append(tuple(float(v) for v in fields[1:4]))
                elif fields[0] == 'f':
                    raw = [int(v.split('/')[0]) for v in fields[1:]]
                    faces.append(tuple(i-1 if i>0 else len(vertices)+i for i in raw))
        else:
            raise ValueError('kinematic_visual_format_unsupported')
        if not len(vertices) or not len(faces):
            raise ValueError('kinematic_visual_empty')
        # local_matrix 可以含 visual mesh scale；烘入顶点，不污染附着坐标。
        points = np.c_[np.asarray(vertices),np.ones(len(vertices))] @ np.asarray(local_matrix).T
        self.create(name, self.pose(parent))
        self._mesh(self.objects[name]+'/visual', points[:,:3].tolist(), faces)
        self.attach(name,parent)

    def pose(self, name):
        return np.asarray(UsdGeom.Xformable(self._prim(name)).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default())).T.tolist()

    def set_pose(self, name, matrix):
        value = np.asarray(matrix,dtype=float)
        if value.shape != (4,4) or not np.all(np.isfinite(value)):
            raise ValueError('kinematic_pose_invalid')
        prim = self._prim(name)
        parent = prim.GetParent()
        parent_world = np.asarray(UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default())).T if parent.IsA(UsdGeom.Xformable) else np.eye(4)
        local = np.linalg.inv(parent_world) @ value
        UsdGeom.Xformable(prim).MakeMatrixXform().Set(Gf.Matrix4d(local.T.tolist()))

    def update(self):
        for prim in self.stage.Traverse():
            if prim.IsA(UsdPhysics.Scene) or prim.HasAPI(UsdPhysics.RigidBodyAPI):
                raise ValueError('kinematic_rigidbody_forbidden')

    def parent(self, name):
        parent = str(self._prim(name).GetParent().GetPath())
        return next((key for key,path in self.objects.items() if path==parent),None)

    def _move(self, name, parent_path):
        old = self.objects[name]
        new = str(parent_path) + '/' + old.rsplit('/',1)[1]
        world = self.pose(name)
        if new.startswith(old+'/'):
            raise ValueError('kinematic_attachment_cycle')
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(old,new)
        layer = self.stage.GetRootLayer()
        if not layer.CanApply(edit) or not layer.Apply(edit):
            raise ValueError('kinematic_reparent_failed')
        for key,path in list(self.objects.items()):
            if path==old or path.startswith(old+'/'):
                self.objects[key]=new+path[len(old):]
        self.set_pose(name,world)
        self.update()

    def attach(self, child, parent):
        if self.parent(child) is not None:
            raise ValueError('kinematic_already_attached')
        self._move(child,self.objects[parent])

    def release(self, child):
        if self.parent(child) is None:
            raise ValueError('kinematic_not_attached')
        self._move(child,'/Kinematic')

    def set_joint_value(self,name,value, *, unit="rad"):
        if not math.isfinite(float(value)):
            raise ValueError('kinematic_joint_nonfinite')
        if unit not in ('rad', 'm'):
            raise ValueError('kinematic_joint_unit_invalid')
        self._prim(name).CreateAttribute('kinematic:jointUnit',Sdf.ValueTypeNames.String).Set(unit)
        self._prim(name).CreateAttribute('kinematic:jointPosition',Sdf.ValueTypeNames.Double).Set(float(value))

    def joint_value(self,name):
        value = self._prim(name).GetAttribute('kinematic:jointPosition').Get()
        if value is None:
            raise ValueError('kinematic_joint_uninitialized')
        return float(value)

    def save(self,path):
        self.update()
        if not self.stage.Export(str(path)):
            raise OSError('kinematic_stage_export_failed')

    import_obj = import_mesh
