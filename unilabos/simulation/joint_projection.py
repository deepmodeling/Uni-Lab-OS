"""把显式关节状态投影至已装配 USD；只做运动学，不声称物理推进。"""
def project_joint_positions(stage,positions,*,limit_tolerance=1e-8):
    """投影 SI 关节状态；limit_tolerance 仅容纳显式的浮点读回误差。

    平移单位 m、旋转单位 rad；容差最多 2e-6，不截断原始坐标，不放宽碰撞。
    """
    import math

    import numpy as np
    from pxr import Gf, UsdGeom, UsdPhysics
    from scipy.spatial.transform import Rotation
    if abs(UsdGeom.GetStageMetersPerUnit(stage)-1)>1e-12:
        raise ValueError("Joint projection requires a metre stage")
    if not math.isfinite(limit_tolerance) or not 0 <= limit_tolerance <= 2e-6:
        raise ValueError("Invalid joint limit roundoff tolerance")
    pending={}
    for path,q in positions.items():
        joint=UsdPhysics.Joint(stage.GetPrimAtPath(path))
        if not joint or not math.isfinite(q):raise ValueError("Invalid joint state: "+path)
        a=list(joint.GetBody0Rel().GetTargets());b=list(joint.GetBody1Rel().GetTargets())
        if len(a)!=1 or len(b)!=1:raise ValueError("Projection requires two explicit joint bodies")
        if not stage.GetPrimAtPath(a[0]) or not stage.GetPrimAtPath(b[0]):raise ValueError("Missing joint body")
        prim=joint.GetPrim()
        if prim.IsA(UsdPhysics.PrismaticJoint):
            typed=UsdPhysics.PrismaticJoint(prim);factor=1.
        elif prim.IsA(UsdPhysics.RevoluteJoint):
            typed=UsdPhysics.RevoluteJoint(prim);factor=math.pi/180.
        else:raise ValueError("Only prismatic/revolute joint states are supported")
        lower,upper=typed.GetLowerLimitAttr().Get()*factor,typed.GetUpperLimitAttr().Get()*factor
        if not lower-limit_tolerance<=q<=upper+limit_tolerance:raise ValueError("Joint state outside authored limits: "+path)
        pending[path]=(joint,float(q),str(a[0]),str(b[0]))
    def local(joint,index):
        result=np.eye(4);rotation=getattr(joint,"GetLocalRot"+str(index)+"Attr")().Get()
        result[:3,:3]=Rotation.from_quat([*rotation.GetImaginary(),rotation.GetReal()]).as_matrix()
        result[:3,3]=getattr(joint,"GetLocalPos"+str(index)+"Attr")().Get()
        return result
    result={}
    while pending:
        targets={row[3] for row in pending.values()}
        ready=[path for path,row in pending.items() if row[2] not in targets]
        if not ready:raise ValueError("Cyclic joint projection")
        for path in ready:
            joint,q,a,b=pending.pop(path)
            motion=np.eye(4);prim=joint.GetPrim()
            if prim.IsA(UsdPhysics.PrismaticJoint):
                axis=UsdPhysics.PrismaticJoint(prim).GetAxisAttr().Get()
                motion["XYZ".index(axis),3]=q
            elif prim.IsA(UsdPhysics.RevoluteJoint):
                axis=UsdPhysics.RevoluteJoint(prim).GetAxisAttr().Get()
                direction=np.eye(3)["XYZ".index(axis)]
                motion[:3,:3]=Rotation.from_rotvec(direction*q).as_matrix()
            else:raise ValueError("Only explicit prismatic/revolute states are supported")
            cache=UsdGeom.XformCache()
            world_a=np.array(cache.GetLocalToWorldTransform(stage.GetPrimAtPath(a))).T
            world=world_a@local(joint,0)@motion@np.linalg.inv(local(joint,1))
            body=stage.GetPrimAtPath(b)
            parent=np.array(cache.GetLocalToWorldTransform(body.GetParent())).T
            xf=UsdGeom.Xformable(body);xf.ClearXformOpOrder()
            attr=body.GetAttribute("xformOp:transform:osJointState")
            op=UsdGeom.XformOp(attr) if attr else xf.AddTransformOp(opSuffix="osJointState")
            op.Set(Gf.Matrix4d((np.linalg.inv(parent)@world).T.tolist()))
            xf.SetXformOpOrder([op])
            result[b]=world.tolist()
    return result


def read_joint_positions(stage,paths):
    """Read SI joint coordinates from the assembled bodies, preserving current children."""
    import numpy as np
    from pxr import UsdGeom, UsdPhysics
    from scipy.spatial.transform import Rotation
    cache=UsdGeom.XformCache();result={}
    def frame(joint,index):
        body=getattr(joint,"GetBody"+str(index)+"Rel")().GetTargets()
        if len(body)!=1:raise ValueError("Joint readback needs two bodies")
        world=np.asarray(cache.GetLocalToWorldTransform(stage.GetPrimAtPath(body[0]))).T
        local=np.eye(4);q=getattr(joint,"GetLocalRot"+str(index)+"Attr")().Get()
        local[:3,:3]=Rotation.from_quat([*q.GetImaginary(),q.GetReal()]).as_matrix()
        local[:3,3]=getattr(joint,"GetLocalPos"+str(index)+"Attr")().Get()
        return world@local
    for path in paths:
        prim=stage.GetPrimAtPath(path);joint=UsdPhysics.Joint(prim)
        relative=np.linalg.inv(frame(joint,0))@frame(joint,1)
        if prim.IsA(UsdPhysics.PrismaticJoint):
            axis="XYZ".index(UsdPhysics.PrismaticJoint(prim).GetAxisAttr().Get())
            residual=relative.copy();q=float(relative[axis,3]);residual[axis,3]=0.
            if not np.allclose(residual,np.eye(4),atol=2e-6):
                raise ValueError("Joint bodies disagree with prismatic frame: "+path)
        elif prim.IsA(UsdPhysics.RevoluteJoint):
            axis="XYZ".index(UsdPhysics.RevoluteJoint(prim).GetAxisAttr().Get())
            v=Rotation.from_matrix(relative[:3,:3]).as_rotvec();q=float(v[axis]);v[axis]=0.
            if np.linalg.norm(v)>2e-6 or np.linalg.norm(relative[:3,3])>2e-6:
                raise ValueError("Joint bodies disagree with revolute frame: "+path)
        else:raise ValueError("Unsupported joint readback")
        result[path]=q
    return result
