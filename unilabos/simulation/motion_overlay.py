"""把统一轨迹采样写入 OS 装配的 USD 动画覆盖层；资产层保持只读。"""
import hashlib
import os
from pathlib import Path

import numpy as np
from pxr import Gf, Usd, UsdGeom


def export_motion_overlay(source,output,samples,metadata,*,robot_visual_root,fps,
                          visual_cutaway_paths=()):
    source=Path(source).resolve();output=Path(output).resolve()
    if source==output:raise ValueError("Motion overlay must not overwrite source assembly")
    if not fps>0:raise ValueError("Positive frame rate required")
    output.parent.mkdir(parents=True,exist_ok=True)
    original=Usd.Stage.Open(str(source))
    if abs(UsdGeom.GetStageMetersPerUnit(original)-1)>1e-12:
        raise ValueError("OS motion overlay requires metre assets")
    up_axis=UsdGeom.GetStageUpAxis(original)
    stage=Usd.Stage.CreateNew(str(output))
    UsdGeom.SetStageMetersPerUnit(stage,1.)
    UsdGeom.SetStageUpAxis(stage,up_axis)
    stage.GetRootLayer().subLayerPaths=[os.path.relpath(source,output.parent).replace(os.sep,"/")]
    if abs(UsdGeom.GetStageMetersPerUnit(stage)-1)>1e-12:
        raise ValueError("OS motion overlay requires metre assets")
    stage.SetTimeCodesPerSecond(fps);stage.SetFramesPerSecond(fps)
    initial={b["path"]:b for b in metadata["bodies"] if b["role"]!="static_world"}
    stretch={path:np.linalg.inv(np.asarray(b["world_from_body_initial"]))@
                   np.asarray(b["usd_world_initial"]) for path,b in initial.items()}
    ops={};times=[];frames=0
    for sample in samples:
        if set(sample["body_poses"])!=set(initial):raise ValueError("Frame body inventory differs from OS geometry")
        timestamp=float(sample["time_s"])*fps
        if times and timestamp<=times[-1]:raise ValueError("Frame times must strictly increase")
        targets={path:np.asarray(pose)@stretch[path] for path,pose in sample["body_poses"].items()}
        targets.update({robot_visual_root+"/"+name:np.asarray(pose) for name,pose in sample["link_poses"].items()})
        if not ops:
            for path in targets:
                prim=stage.GetPrimAtPath(path)
                if not prim or not prim.IsActive():raise ValueError("Missing OS animation target: "+path)
                xf=UsdGeom.Xformable(prim)
                ops[path]=xf.AddTransformOp(opSuffix="osTrajectory")
                # Importers may detach USD rigid bodies from their parents.
                # World-space reset stacks keep every consumer unambiguous.
                xf.SetXformOpOrder([ops[path]],resetXformStack=True)
        if set(targets)!=set(ops):raise ValueError("Animation target inventory changed")
        for path,matrix in targets.items():
            ops[path].Set(Gf.Matrix4d(matrix.T.tolist()),Usd.TimeCode(timestamp))
        times.append(timestamp);frames+=1
    if not times:raise ValueError("No motion frames")
    hidden=[]
    for path in visual_cutaway_paths:
        prim=stage.GetPrimAtPath(path)
        if not prim:raise ValueError("Unknown visual cutaway path: "+path)
        UsdGeom.Imageable(prim).CreateVisibilityAttr("invisible");hidden.append(path)
    stage.SetStartTimeCode(times[0]);stage.SetEndTimeCode(times[-1])
    stage.GetRootLayer().Save()
    return dict(source_assembly=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                overlay=str(output),frames=frames,fps=fps,start_time_code=times[0],end_time_code=times[-1],
                animated_objects=len(ops),world_animation_paths=sorted(ops),
                animation_space="world_reset_xform_stack",visual_cutaway_paths=hidden,
                geometry_source="unchanged OS assembly sublayer",physics=False)
