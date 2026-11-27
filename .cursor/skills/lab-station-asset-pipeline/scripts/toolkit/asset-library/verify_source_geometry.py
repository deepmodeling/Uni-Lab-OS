"""Independent source-vs-USD world bounds checks for STEP and box-only URDF."""
import argparse,itertools,math
from pathlib import Path
import numpy as np
from core import write,digest

def trimmed_step_bounds(shape, enclosing_m):
    """Measure six CAD supports on the trimmed shape, independently of the USD mesh."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Dir,gp_Pln,gp_Pnt
    box=enclosing_m*1000
    margin=max(1.,float(np.max(box[1]-box[0])))
    result=np.empty((2,3))
    for axis,normal in enumerate(np.eye(3)):
        for side,sign in enumerate((-1,1)):
            center=box.mean(axis=0)
            center[axis]=box[side,axis]+sign*margin
            plane=gp_Pln(gp_Pnt(*center),gp_Dir(*normal))
            face=BRepBuilderAPI_MakeFace(plane,-2*margin,2*margin,-2*margin,2*margin).Face()
            distance=BRepExtrema_DistShapeShape(shape,face)
            if not distance.IsDone() or not distance.NbSolution():
                raise ValueError('Cannot measure trimmed STEP bounds')
            support=center[axis]-sign*distance.Value()
            if abs(support-distance.PointOnShape1(1).Coord()[axis])>1e-5:
                raise ValueError('STEP support point does not agree with plane distance')
            result[side,axis]=support*.001
    return result

def verify(source,usd,family,tolerance_m=0.0001):
    from pxr import Usd,UsdGeom,Gf
    if not math.isfinite(tolerance_m) or tolerance_m<=0:raise ValueError('Invalid tolerance')
    source=Path(source);usd=Path(usd)
    if family=='step':
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        reader=STEPControl_Reader()
        if reader.ReadFile(str(source))!=IFSelect_RetDone:raise ValueError('STEP read failed')
        reader.SetSystemLengthUnit(1.);reader.TransferRoots();shape=reader.OneShape();box=Bnd_Box()
        BRepBndLib.AddOptimal_s(shape,box,False,False);expected=np.array(box.Get()).reshape(2,3)*0.001
    elif family=='urdf':
        import xml.etree.ElementTree as ET
        robot=ET.parse(source).getroot();parents={j.find('child').get('link'):j for j in robot.findall('joint')};busy=set()
        def matrix(origin):
            m=np.eye(4)
            if origin is None:return m
            x,y,z=map(float,origin.get('rpy','0 0 0').split());cx,sx,cy,sy,cz,sz=math.cos(x),math.sin(x),math.cos(y),math.sin(y),math.cos(z),math.sin(z)
            m[:3,:3]=np.array([[cz,-sz,0],[sz,cz,0],[0,0,1]])@np.array([[cy,0,sy],[0,1,0],[-sy,0,cy]])@np.array([[1,0,0],[0,cx,-sx],[0,sx,cx]])
            m[:3,3]=list(map(float,origin.get('xyz','0 0 0').split()));return m
        def world(name):
            if name not in parents:return np.eye(4)
            if name in busy:raise ValueError('URDF cycle')
            busy.add(name);j=parents[name];m=world(j.find('parent').get('link'))@matrix(j.find('origin'));busy.remove(name);return m
        points=[]
        for link in robot.findall('link'):
            for visual in link.findall('visual'):
                box=visual.find('geometry/box')
                if box is None:raise ValueError('Independent URDF check supports boxes only')
                size=np.array(list(map(float,box.get('size').split())));m=world(link.get('name'))@matrix(visual.find('origin'))
                for signs in itertools.product((-0.5,0.5),repeat=3):points.append((m@np.r_[size*np.array(signs),1])[:3])
        expected=np.array([np.min(points,axis=0),np.max(points,axis=0)])
    else:raise ValueError('Unsupported independent source check')
    stage=Usd.Stage.Open(str(usd));cache=UsdGeom.XformCache();points=[]
    for p in Usd.PrimRange.Stage(stage,Usd.TraverseInstanceProxies()):
        if p.IsA(UsdGeom.Mesh) or (family=='step' and (p.IsA(UsdGeom.BasisCurves) or p.IsA(UsdGeom.Points))):
            matrix=cache.GetLocalToWorldTransform(p)
            points.extend(tuple(matrix.Transform(Gf.Vec3d(*v))) for v in UsdGeom.PointBased(p).GetPointsAttr().Get())
    actual=np.array([np.min(points,axis=0),np.max(points,axis=0)])*UsdGeom.GetStageMetersPerUnit(stage)
    # These routes retain source Z-up; reject a coordinate convention mismatch.
    if str(UsdGeom.GetStageUpAxis(stage))!='Z':raise ValueError('Expected source Z-up frame')
    delta=float(np.max(np.abs(expected-actual)))
    details={}
    if family=='step' and delta>tolerance_m:
        # AddOptimal can enclose the underlying surface outside its trimming wires.
        details={'enclosing_bounds_m':expected.tolist(),'enclosing_error_m':delta,'bounds_method':'trimmed CAD distance to six enclosing planes'}
        expected=trimmed_step_bounds(shape,expected)
        delta=float(np.max(np.abs(expected-actual)))
    return {'status':'passed' if delta<=tolerance_m else 'failed','family':family,'source_sha256':digest(source),'usd_sha256':digest(usd),'source_bounds_m':expected.tolist(),'usd_bounds_m':actual.tolist(),'max_error_m':delta,'tolerance_m':tolerance_m,'scope':'independent whole-asset source bounds; does not certify every face or dynamics',**details}

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for k in ('source','usd','family','output'):p.add_argument('--'+k,required=True)
    a=p.parse_args();r=verify(a.source,a.usd,a.family);write(a.output,r);print(r);raise SystemExit(0 if r['status']=='passed' else 1)
