"""把设备包提供的独立 URDF 树按 OS 装配图连接；不包含型号知识。"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET


def assemble_robot_model(plan, bundles, *, attachment_links=None):
    import numpy as np
    from scipy.spatial.transform import Rotation
    attachment_links = dict(attachment_links or {})
    if set(attachment_links) - set(bundles):
        raise ValueError("Attachment override has no child model")
    robot=ET.Element("robot",name="os_workcell")
    ET.SubElement(robot,"link",name="world")
    semantic=ET.Element("robot",name="os_workcell")
    links={"world"};joints=set()
    def pose(identity):
        placement=plan.placements[identity]
        matrix=np.eye(4);matrix[:3,:3]=np.array(placement.world_rotation_row_major).T
        matrix[:3,3]=np.array(placement.world_mm)/1000
        return matrix
    def vector(values):
        return " ".join(format(float(v),".12g") for v in values)
    for identity in plan.scene.order:
        if identity not in bundles:continue
        bundle=bundles[identity]
        tree=ET.fromstring(getattr(bundle,"execution_urdf",None) or bundle.render_urdf)
        parent=plan.scene.nodes[identity].parent
        parent_bundle=bundles.get(parent)
        parent_link=attachment_links.get(identity, getattr(parent_bundle,"mount_link","") or "world")
        if identity in attachment_links:
            if parent_bundle is None:
                raise ValueError("Attachment override requires a parent model")
            parent_tree=ET.fromstring(getattr(parent_bundle,"execution_urdf",None) or parent_bundle.render_urdf)
            if parent_link not in {link.get("name") for link in parent_tree.findall("link")}:
                raise ValueError("Attachment link is not owned by the declared parent")
        local=np.linalg.inv(pose(parent))@pose(identity) if parent_link!="world" else pose(identity)
        mounts=[j for j in tree.findall("joint") if j.find("parent").get("link")=="world"]
        if len(mounts)>1:raise ValueError("Multiple provider world mounts: "+identity)
        if mounts:
            mount=mounts[0]
            if mount.get("type")!="fixed":raise ValueError("Provider world mount must be fixed")
        else:
            child_links={j.find("child").get("link") for j in tree.findall("joint")}
            roots={l.get("name") for l in tree.findall("link")}-{*child_links,"world"}
            if len(roots)!=1:raise ValueError("Provider must have one root")
            mount=ET.SubElement(tree,"joint",name=identity+"_graph_mount",type="fixed")
            ET.SubElement(mount,"parent",link=parent_link)
            ET.SubElement(mount,"child",link=roots.pop())
        mount.find("parent").set("link",parent_link)
        origin=mount.find("origin")
        if origin is None:origin=ET.SubElement(mount,"origin")
        origin.set("xyz",vector(local[:3,3]))
        origin.set("rpy",vector(Rotation.from_matrix(local[:3,:3]).as_euler("xyz")))
        for element in tree:
            if element.tag=="link":
                name=element.get("name")
                if name=="world":continue
                if name in links:raise ValueError("Duplicate link: "+name)
                links.add(name)
            elif element.tag=="joint":
                name=element.get("name")
                if name in joints:raise ValueError("Duplicate joint: "+name)
                joints.add(name)
            robot.append(copy.deepcopy(element))
        if getattr(bundle,"srdf",None):
            for element in ET.fromstring(bundle.srdf):semantic.append(copy.deepcopy(element))
    for joint in robot.findall("joint"):
        for tag in ("parent","child"):
            if joint.find(tag).get("link") not in links:raise ValueError("Unknown URDF attachment link")
    return ET.tostring(robot,encoding="unicode"),ET.tostring(semantic,encoding="unicode")


def robot_link_poses(urdf,positions):
    """从同一装配 URDF 计算每个连接的世界姿态，不推测缺失的轴状态。"""
    import numpy as np
    from scipy.spatial.transform import Rotation
    tree=ET.fromstring(urdf)
    def origin(element):
        result=np.eye(4)
        if element is not None:
            result[:3,3]=[float(x) for x in element.get("xyz","0 0 0").split()]
            result[:3,:3]=Rotation.from_euler("xyz",[float(x) for x in element.get("rpy","0 0 0").split()]).as_matrix()
        return result
    joints=tree.findall("joint");moving={j.get("name") for j in joints if j.get("type")!="fixed"}
    if set(positions)!=moving:raise ValueError("Explicit states must cover all moving URDF joints")
    roots={l.get("name") for l in tree.findall("link")}-{j.find("child").get("link") for j in joints}
    if len(roots)!=1:raise ValueError("Expected one URDF root")
    poses={roots.pop():np.eye(4)};pending=list(joints)
    while pending:
        ready=[j for j in pending if j.find("parent").get("link") in poses]
        if not ready:raise ValueError("Disconnected or cyclic URDF")
        for joint in ready:
            pending.remove(joint);motion=np.eye(4);kind=joint.get("type")
            if kind!="fixed":
                q=float(positions[joint.get("name")])
                if not np.isfinite(q):raise ValueError("Nonfinite joint position")
                limit=joint.find("limit")
                if limit is not None and not float(limit.get("lower","-inf"))<=q<=float(limit.get("upper","inf")):
                    raise ValueError("Joint position outside provider limits")
                axis=joint.find("axis")
                vector=np.array([float(x) for x in (axis.get("xyz") if axis is not None else "1 0 0").split()])
                if np.linalg.norm(vector)==0:raise ValueError("Zero joint axis")
                vector/=np.linalg.norm(vector)
                if kind=="prismatic":motion[:3,3]=vector*q
                elif kind in ("revolute","continuous"):motion[:3,:3]=Rotation.from_rotvec(vector*q).as_matrix()
                else:raise ValueError("Unsupported joint type")
            poses[joint.find("child").get("link")]=poses[joint.find("parent").get("link")]@origin(joint.find("origin"))@motion
    return poses

def project_robot_visuals(stage,root_path,urdf,positions):
    """使用官方 URDF 原始三角网格投影外观，碰撞仍由同一 URDF 提供。"""
    import hashlib
    from pathlib import Path
    from urllib.parse import unquote, urlparse

    import numpy as np
    from pxr import Gf, Sdf, UsdGeom, Vt
    from scipy.spatial.transform import Rotation
    poses=robot_link_poses(urdf,positions);tree=ET.fromstring(urdf)
    root=UsdGeom.Xform.Define(stage,root_path)
    parent=np.array(root.ComputeLocalToWorldTransform(0)).T
    receipt={"meshes":[],"collision_authority":"same provider URDF","urdf_sha256":hashlib.sha256(urdf.encode()).hexdigest()}
    for link in tree.findall("link"):
        name=link.get("name");path=root_path+"/"+name
        prim=UsdGeom.Xform.Define(stage,path);prim.ClearXformOpOrder()
        # 更新时复用同名变换属性。
        op=next((o for o in prim.GetOrderedXformOps() if o.GetOpName()=="xformOp:transform:osModel"),None)
        if op is None:
            attr=prim.GetPrim().GetAttribute("xformOp:transform:osModel")
            op=UsdGeom.XformOp(attr) if attr else prim.AddTransformOp(opSuffix="osModel")
        op.Set(Gf.Matrix4d((np.linalg.inv(parent)@poses[name]).T.tolist()));prim.SetXformOpOrder([op])
        for index,visual in enumerate(link.findall("visual")):
            specification=visual.find("geometry/mesh")
            if specification is None:raise ValueError("Visual projection currently requires exact mesh")
            source=specification.get("filename")
            if source.startswith("file://"):
                source=unquote(urlparse(source).path)
                if len(source)>2 and source[0]=="/" and source[2]==":":source=source[1:]
            if "://" in source:raise ValueError("Resolve provider mesh URI before projection")
            file=Path(source);raw=file.read_bytes()
            mesh_path=path+"/visual_"+str(index)
            mesh=UsdGeom.Mesh.Get(stage,mesh_path)
            if not mesh:
                points,faces=_read_stl(raw)
                points=points*np.array([float(x) for x in specification.get("scale","1 1 1").split()])
                local=np.eye(4);o=visual.find("origin")
                if o is not None:
                    local[:3,3]=[float(x) for x in o.get("xyz","0 0 0").split()]
                    local[:3,:3]=Rotation.from_euler("xyz",[float(x) for x in o.get("rpy","0 0 0").split()]).as_matrix()
                points=points@local[:3,:3].T+local[:3,3]
                mesh=UsdGeom.Mesh.Define(stage,mesh_path)
                mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(points.astype(np.float32)))
                mesh.CreateFaceVertexCountsAttr([3]*len(faces))
                mesh.CreateFaceVertexIndicesAttr(faces.flatten().tolist())
                mesh.CreateSubdivisionSchemeAttr("none")
                mesh.CreateDisplayColorAttr([(0.7,0.72,0.75)])
                mesh.GetPrim().CreateAttribute("lab:providerMeshSHA256",Sdf.ValueTypeNames.String).Set(hashlib.sha256(raw).hexdigest())
            receipt["meshes"].append(dict(link=name,path=str(file),sha256=hashlib.sha256(raw).hexdigest()))
    return receipt


def _read_stl(raw):
    """保留 STL 的原始三角顶点，不合并、不抽稀、不修补。"""
    import struct

    import numpy as np
    count=struct.unpack_from("<I",raw,80)[0] if len(raw)>=84 else -1
    if count>=0 and len(raw)==84+50*count:
        dtype=np.dtype([("normal","<f4",(3,)),("vertices","<f4",(3,3)),("attribute","<u2")])
        points=np.frombuffer(raw,offset=84,dtype=dtype,count=count)["vertices"].reshape(-1,3).astype(np.float64)
    else:
        try:
            lines=[line.split()[1:] for line in raw.decode("ascii").splitlines() if line.strip().startswith("vertex ")]
            points=np.array([[float(v) for v in row] for row in lines],dtype=np.float64)
        except (UnicodeError,ValueError) as error:raise ValueError("Unsupported or malformed STL") from error
    if points.ndim!=2 or points.shape[1]!=3 or not len(points) or len(points)%3 or not np.isfinite(points).all():
        raise ValueError("Invalid STL triangle vertices")
    return points,np.arange(len(points),dtype=np.int32).reshape(-1,3)
