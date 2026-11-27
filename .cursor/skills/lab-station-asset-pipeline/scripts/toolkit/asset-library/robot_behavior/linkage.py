"""Offline graphical linkage evaluation; no vendor runtime or dynamics claims.

Inputs are column-vector link transforms in metres. This function is shipped
with each merged asset so the dependency is usable outside the converter.
"""
import math


def auxiliary_poses(contract, link_poses):
    if contract['kind'] != 'robodk_two_anchor_slide':
        raise ValueError('Unsupported auxiliary linkage contract')
    def point(matrix, xyz):
        return [sum(matrix[i][j]*xyz[j] for j in range(3))+matrix[i][3] for i in range(3)]
    def cross(a,b):
        return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
    def unit(v):
        length=math.sqrt(sum(x*x for x in v))
        if not math.isfinite(length) or length<1e-12:raise ValueError('Singular auxiliary linkage')
        return [x/length for x in v]
    a,b=[point(link_poses[n],xyz) for n,xyz in zip(contract['anchor_links'],contract['anchors_m'])]
    x=unit([b[i]-a[i] for i in range(3)])
    reference=link_poses[contract['orientation_link']]
    z=unit(cross(x,[reference[i][contract['orientation_column']] for i in range(3)]));y=cross(z,x)
    return [[[x[i],y[i],z[i],p[i]] for i in range(3)]+[[0.,0.,0.,1.]] for p in (a,b)]


def apply_auxiliary_poses(stage, contract, link_poses, root='/Asset'):
    """Accept world poses in metres; author child transforms relative to root."""
    from pxr import UsdGeom,Gf
    parent=stage.GetPrimAtPath(root)
    if not parent:raise ValueError('Missing auxiliary linkage root')
    parent_inverse=UsdGeom.XformCache().GetLocalToWorldTransform(parent).GetInverse()
    poses=auxiliary_poses(contract,link_poses)
    for index,pose in zip(contract['output_links'],poses):
        xf=UsdGeom.Xformable(stage.GetPrimAtPath(root+'/link_'+str(index)))
        if not xf:raise ValueError('Missing auxiliary source link')
        world=Gf.Matrix4d([list(row) for row in zip(*pose)])
        xf.MakeMatrixXform().Set(world*parent_inverse)
    return poses
