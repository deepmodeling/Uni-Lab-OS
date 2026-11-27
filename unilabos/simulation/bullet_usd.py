"""Original USD collision shapes -> Blender's built-in Bullet C API."""
import json
from pathlib import Path

import numpy as np
from mathutils import Matrix, Vector
from pxr import Usd, UsdGeom, UsdPhysics

from .bullet_native import *


def matrix(prim):return np.asarray(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T

def points(prim):
    if prim.IsA(UsdGeom.Mesh):return np.asarray(UsdGeom.Mesh(prim).GetPointsAttr().Get())
    if prim.IsA(UsdGeom.Cube):
        size=UsdGeom.Cube(prim).GetSizeAttr().Get()/2
        return np.array([(x,y,z) for x in (-size,size) for y in (-size,size) for z in (-size,size)])
    raise ValueError('Unhandled original collider '+str(prim.GetPath()))

def shape(prim,transform):
    verts=np.ascontiguousarray((points(prim)@transform[:3,:3].T+transform[:3,3]),dtype=np.float32)
    approx=UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get() if prim.IsA(UsdGeom.Mesh) else 'convexHull'
    if approx in ('sdf','none'):
        geo=UsdGeom.Mesh(prim);indices=np.asarray(geo.GetFaceVertexIndicesAttr().Get());counts=np.asarray(geo.GetFaceVertexCountsAttr().Get());faces=[];start=0
        for n in counts:
            v=indices[start:start+n];faces.extend((int(v[0]),int(v[i]),int(v[i+1])) for i in range(1,n-1));start+=n
        mesh=B.RB_trimesh_data_new(len(faces),len(verts));B.RB_trimesh_add_vertices(mesh,verts.ctypes.data,len(verts),12)
        for i,f in enumerate(faces):B.RB_trimesh_add_triangle_indices(mesh,i,*f)
        B.RB_trimesh_finish(mesh);s=B.RB_shape_new_gimpact_mesh(mesh)
    else:
        embedded=C.c_bool(True);s=B.RB_shape_new_convex_hull(verts.ctypes.data,12,len(verts),0,C.byref(embedded))
    B.RB_shape_set_margin(s,.00005);return s

class Body:
    def __init__(self,world,name,prim,world_from_root,static=False,mask=1):
        mass=UsdPhysics.MassAPI(prim);self.mass=float(mass.GetMassAttr().Get() or 0)
        self.com=np.eye(4);com=mass.GetCenterOfMassAttr().Get()
        if com is not None and np.isfinite(np.asarray(com)).all():self.com[:3,3]=np.asarray(com)
        axes=mass.GetPrincipalAxesAttr().Get()
        if axes is not None:
            from mathutils import Quaternion
            self.com[:3,:3]=np.array(Quaternion((axes.GetReal(),*np.asarray(axes.GetImaginary()))).to_matrix())
        colliders=[p for p in Usd.PrimRange(prim) if p.HasAPI(UsdPhysics.CollisionAPI) and UsdPhysics.CollisionAPI(p).GetCollisionEnabledAttr().Get()!=False]
        compound=B.RB_shape_new_compound();inv=np.linalg.inv(matrix(prim));icom=np.linalg.inv(self.com)
        for p in colliders:
            s=shape(p,icom@inv@matrix(p));B.RB_compound_add_child_shape(compound,s,V3(0,0,0),V4(1,0,0,0))
        if not colliders:
            s=B.RB_shape_new_box(.0001,.0001,.0001);B.RB_compound_add_child_shape(compound,s,V3(0,0,0),V4(1,0,0,0));mask=0
        self.ptr=world.body(name,compound,world_from_root@self.com,0 if static else self.mass,mask)
        inertia=mass.GetDiagonalInertiaAttr().Get()
        if not static and inertia is not None and min(inertia)>0:E.lab_inertia(self.ptr,self.mass,*inertia)
        self.name=name;self.colliders=len(colliders)
    def pose(self):return pose(self.ptr)@np.linalg.inv(self.com)
    def velocity(self):return velocity(self.ptr)

def constraint(world,kind,parent,child,pivot,axis=None):
    if axis is None:q=V4(1,0,0,0)
    else:q=V4(*Vector((0,0,1) if kind=='hinge' else (1,0,0)).rotation_difference(Vector(axis)))
    ptr=getattr(B,'RB_constraint_new_'+kind)(V3(*pivot),q,parent,child)
    B.RB_dworld_add_constraint(world.ptr,ptr,1)
    B.RB_constraint_set_solver_iterations(ptr,1000)
    if kind=='hinge':B.RB_constraint_set_limits_hinge(ptr,1,-1)
    if kind=='slider':B.RB_constraint_set_limits_slider(ptr,1,-1)
    return ptr
