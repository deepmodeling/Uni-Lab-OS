"""Generic OS candidate-state projection, without hardware or physical custody claims."""
import numpy as np
from pxr import Gf, UsdGeom

from .collision_snapshot import read_collision_body_poses
from .scene_robot_model import robot_link_poses


def set_world_transform(stage,path,matrix):
    prim=stage.GetPrimAtPath(path)
    if not prim or not prim.IsActive():raise ValueError("Missing state target: "+path)
    parent=UsdGeom.XformCache().GetLocalToWorldTransform(prim.GetParent())
    local=np.linalg.inv(np.asarray(parent).T)@np.asarray(matrix)
    xf=UsdGeom.Xformable(prim)
    attr=prim.GetAttribute("xformOp:transform:osCandidate")
    op=UsdGeom.XformOp(attr) if attr else xf.AddTransformOp(opSuffix="osCandidate")
    op.Set(Gf.Matrix4d(local.T.tolist()));xf.SetXformOpOrder([op])

class KinematicSceneState:
    def __init__(self,stage,metadata,urdf,positions,*,robot_visual_root,bindings):
        self.stage,self.metadata,self.urdf=stage,metadata,urdf
        self.robot_visual_root=robot_visual_root
        stage.SetEditTarget(stage.GetSessionLayer())
        self.positions=dict(positions);self.bindings={}
        initial=read_collision_body_poses(stage,metadata)
        self.stretch={b["path"]:np.linalg.inv(np.asarray(b["world_from_body_initial"]))@
            np.asarray(b["usd_world_initial"]) for b in metadata["bodies"] if b["role"]!="static_world"}
        for body,link in bindings.items():self.bind(body,link,poses=initial)
        self.set_joints(positions)

    def bind(self,body,link,*,poses=None):
        if body in self.bindings:raise ValueError("Body already attached")
        poses=poses or read_collision_body_poses(self.stage,self.metadata)
        links=robot_link_poses(self.urdf,self.positions)
        if body not in poses or link not in links:raise ValueError("Unknown body or link")
        self.bindings[body]=(link,np.linalg.inv(links[link])@np.asarray(poses[body]))

    def release(self,body):
        if body not in self.bindings:raise ValueError("Body is not attached")
        del self.bindings[body]

    def recapture_binding(self,body):
        link=self.bindings[body][0];self.release(body);self.bind(body,link)

    def set_joints(self,positions):
        links=robot_link_poses(self.urdf,positions)
        # Validate all paths before the first mutation.
        targets={self.robot_visual_root+"/"+name:matrix for name,matrix in links.items()}
        targets.update({body:links[link]@relative@self.stretch[body]
                        for body,(link,relative) in self.bindings.items()})
        if any(not self.stage.GetPrimAtPath(p) for p in targets):
            raise ValueError("OS robot projection is incomplete")
        for path,matrix in targets.items():set_world_transform(self.stage,path,matrix)
        self.positions=dict(positions)

    def body_poses(self):
        return read_collision_body_poses(self.stage,self.metadata)


    def attachment_snapshot(self):
        """Serialize authoritative local mounts; joint motion must not change them.

        Revision is a content identity, not a native PlanningScene receipt.
        Consumers must still check the projected poses against their current state.
        """
        import hashlib
        import json
        value = {"schema": "unilab.attachment-state/v1",
                 "robot_urdf_sha256": hashlib.sha256(self.urdf.encode()).hexdigest(),
                 "bindings": {body: {"link_name": link, "link_from_body": relative.tolist()}
                              for body, (link, relative) in sorted(self.bindings.items())}}
        value["revision"] = hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return value

    def restore_attachment_snapshot(self, snapshot):
        """Restore exact mounts only after checking model, inventory and USD projection."""
        import hashlib
        import json
        value = {key: item for key, item in snapshot.items() if key != "revision"}
        digest = hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if (value.get("schema") != "unilab.attachment-state/v1"
                or digest != snapshot.get("revision")
                or value.get("robot_urdf_sha256") != hashlib.sha256(self.urdf.encode()).hexdigest()):
            raise ValueError("Attachment snapshot identity differs")
        links = robot_link_poses(self.urdf, self.positions)
        poses = self.body_poses()
        restored = {}
        for body, binding in value["bindings"].items():
            link = binding["link_name"]
            relative = np.asarray(binding["link_from_body"], dtype=float)
            if body not in poses or link not in links:
                raise ValueError("Unknown attachment body or link")
            if (relative.shape != (4, 4) or not np.isfinite(relative).all()
                    or not np.allclose(relative[3], [0, 0, 0, 1], rtol=0, atol=1e-12)
                    or not np.allclose(relative[:3,:3].T @ relative[:3,:3], np.eye(3), rtol=0, atol=1e-8)
                    or abs(np.linalg.det(relative[:3,:3])-1) > 1e-8
                    or not np.allclose(links[link] @ relative, poses[body], rtol=0, atol=1e-8)):
                raise ValueError("Attachment snapshot projection differs")
            restored[body] = (link, relative.copy())
        self.bindings = restored

    def checkpoint(self):
        """Save only the candidate session layer and runtime bindings."""
        import copy
        return {"session_layer":self.stage.GetSessionLayer().ExportToString(),
                "positions":dict(self.positions),"bindings":copy.deepcopy(self.bindings)}

    def restore(self,checkpoint):
        import copy
        if not self.stage.GetSessionLayer().ImportFromString(checkpoint["session_layer"]):
            raise ValueError("Cannot restore OS candidate session layer")
        self.positions=dict(checkpoint["positions"])
        self.bindings=copy.deepcopy(checkpoint["bindings"])
