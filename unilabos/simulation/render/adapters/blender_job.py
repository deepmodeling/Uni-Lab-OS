"""在 Blender 里执行的出图/编码/验收脚本（``blender -b --python blender_job.py -- job.json``）。

不导入 unilabos：只用 bpy 与 Blender 自带的 pxr。job.json 由 ``unilabos.simulation.render.adapters.blender``
写出，``mode`` 取 ``render``（出一个分片的 PNG）、``encode``（PNG 序列 → MP4）、``probe``（读回成片帧数/帧率）。

关节摆位与前端 usdJoints 相同：body1_world = body0_world · F0 · Δ(q) · F1⁻¹，F0/F1 是关节在两侧
刚体里的局部坐标系（localPos/localRot），Δ(q) 是绕/沿关节轴的转动或平移；转动关节的值按弧度、
平移关节按米（与观测描述一致）。
"""

import json
import math
import os
import re
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    with open(argv[0], encoding="utf-8") as stream:
        return json.load(stream)


def _clear():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _matrix_mm(rows):
    m = Matrix([row[:] for row in rows])
    m.translation = Vector([v / 1000.0 for v in (rows[0][3], rows[1][3], rows[2][3])])
    return m


def _strip(name):
    return re.sub(r"\.\d{3}$", "", name)


def _load_joints(usd_path):
    from pxr import Usd, UsdPhysics  # Blender 自带

    stage = Usd.Stage.Open(usd_path)
    joints = {}
    for prim in stage.Traverse():
        kind = "revolute" if prim.IsA(UsdPhysics.RevoluteJoint) else "prismatic" if prim.IsA(UsdPhysics.PrismaticJoint) else None
        if kind is None:
            continue
        joint = UsdPhysics.Joint(prim)
        body0 = [str(t) for t in joint.GetBody0Rel().GetTargets()]
        body1 = [str(t) for t in joint.GetBody1Rel().GetTargets()]
        if not body1:
            continue

        def frame(pos, rot):
            pos = pos.Get() or (0, 0, 0)
            rot = rot.Get()
            q = Quaternion((rot.GetReal(), *rot.GetImaginary())) if rot is not None else Quaternion()
            return Matrix.Translation(Vector(pos)) @ q.to_matrix().to_4x4()

        axis = (prim.GetAttribute("physics:axis").Get() or "X")
        joints[prim.GetName()] = {
            "kind": kind, "axis": {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[str(axis)],
            "body0": body0[0].rsplit("/", 1)[-1] if body0 else None, "body1": body1[0].rsplit("/", 1)[-1],
            "f0": frame(joint.GetLocalPos0Attr(), joint.GetLocalRot0Attr()),
            "f1": frame(joint.GetLocalPos1Attr(), joint.GetLocalRot1Attr()),
        }
    return joints


def _import_node(item):
    before = set(bpy.data.objects)
    bpy.ops.wm.usd_import(filepath=item["usd"], import_cameras=False, import_lights=False)
    created = [obj for obj in bpy.data.objects if obj not in before]
    root = bpy.data.objects.new(f"node:{item['node']}", None)
    bpy.context.scene.collection.objects.link(root)
    for obj in created:
        if obj.parent is None:
            world = obj.matrix_world.copy()
            obj.parent = root
            obj.matrix_world = world
    root.matrix_world = _matrix_mm(item["world_mm"])
    bpy.context.view_layer.update()
    by_name = {}
    for obj in created:
        by_name.setdefault(_strip(obj.name), obj)
    joints = _load_joints(item["usd"]) if item.get("joints") else {}
    # 关节按父→子顺序：body0 先摆好再摆 body1。
    order, placed = [], set()
    pending = dict(joints)
    while pending:
        ready = [name for name, j in pending.items() if j["body0"] not in {k["body1"] for k in pending.values()}]
        if not ready:
            ready = list(pending)
        for name in ready:
            order.append(name)
            placed.add(name)
            pending.pop(name)
    return {"root": root, "objects": by_name, "joints": joints, "order": order,
            "property_of": {joint: prop for prop, joint in (item.get("joints") or {}).items()}}


def _apply(node, values):
    for name in node["order"]:
        joint = node["joints"][name]
        body0 = node["objects"].get(joint["body0"]) if joint["body0"] else None
        body1 = node["objects"].get(joint["body1"])
        if body1 is None:
            continue
        prop = node["property_of"].get(name, name)
        q = values.get(prop)
        q = float(q) if isinstance(q, (int, float)) else 0.0
        axis = Vector(joint["axis"])
        delta = (Matrix.Rotation(q, 4, axis) if joint["kind"] == "revolute" else Matrix.Translation(axis * q))
        parent = body0.matrix_world if body0 is not None else node["root"].matrix_world
        body1.matrix_world = parent @ joint["f0"] @ delta @ joint["f1"].inverted()
    bpy.context.view_layer.update()


def _camera(shots, frame):
    shot = next((s for s in shots if s["f_start"] <= frame < s["f_end"]), shots[-1])
    target = Vector([v / 1000.0 for v in shot["target_mm"]])
    distance = shot["distance_mm"] / 1000.0
    elevation, azimuth = math.radians(shot["elevation_deg"]), math.radians(shot["azimuth_deg"])
    offset = Vector((math.cos(elevation) * math.cos(azimuth), math.cos(elevation) * math.sin(azimuth), math.sin(elevation)))
    camera = bpy.data.objects["render-camera"]
    camera.location = target + offset * distance
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()


def render(job):
    _clear()
    scene = bpy.context.scene
    with open(job["scene"], encoding="utf-8") as stream:
        spec = json.load(stream)
    with open(job["timeline"], encoding="utf-8") as stream:
        timeline = json.load(stream)
    nodes = {item["node"]: _import_node(item) for item in spec["nodes"]}
    camera_data = bpy.data.cameras.new("render-camera")
    camera_data.lens = 35
    camera = bpy.data.objects.new("render-camera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    sun = bpy.data.objects.new("render-sun", bpy.data.lights.new("render-sun", "SUN"))
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    scene.render.engine = job.get("render_engine", "BLENDER_WORKBENCH")
    scene.render.resolution_x, scene.render.resolution_y = job["resolution"]
    scene.render.image_settings.file_format = "PNG"
    world = bpy.data.worlds.new("render-world")
    world.color = (0.92, 0.93, 0.95)
    scene.world = world

    state = {}
    cursor = 0
    frames = timeline["frames"]
    shard = job["shard"]
    os.makedirs(job["out_dir"], exist_ok=True)
    written = 0
    for frame in range(shard["f_end"]):
        while cursor < len(frames) and frames[cursor]["f"] <= frame:
            for node_id, values in frames[cursor]["set"].items():
                state.setdefault(node_id, {}).update(values)
            cursor += 1
        if frame < shard["f_start"]:
            continue
        for node_id, node in nodes.items():
            _apply(node, state.get(node_id, {}))
        _camera(job["shots"], frame)
        scene.render.filepath = os.path.join(job["out_dir"], f"frame_{frame:06d}.png")
        bpy.ops.render.render(write_still=True)
        written += 1
    with open(os.path.join(job["out_dir"], "shard.json"), "w", encoding="utf-8") as stream:
        json.dump({"index": shard["index"], "frames": written}, stream)


def encode(job):
    _clear()
    scene = bpy.context.scene
    directory = job["frames_dir"]
    files = sorted(f for f in os.listdir(directory) if f.endswith(".png"))
    scene.sequence_editor_create()
    editor = scene.sequence_editor
    strips = editor.strips if hasattr(editor, "strips") else editor.sequences  # Blender 5 改名为 strips
    strip = strips.new_image("frames", os.path.join(directory, files[0]), 1, 1)
    for name in files[1:]:
        strip.elements.append(name)
    scene.frame_start, scene.frame_end = 1, len(files)
    scene.render.fps = job["fps"]
    scene.render.resolution_x, scene.render.resolution_y = job["resolution"]
    settings = scene.render.image_settings
    if hasattr(settings, "media_type"):  # Blender 5：视频输出要先切到 VIDEO
        settings.media_type = "VIDEO"
    settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "HIGH"
    scene.render.filepath = job["output"]
    bpy.ops.render.render(animation=True)


def probe(job):
    _clear()
    clip = bpy.data.movieclips.load(job["video"])
    with open(job["result"], "w", encoding="utf-8") as stream:
        json.dump({"frames": clip.frame_duration, "fps": clip.fps}, stream)


if __name__ == "__main__":
    JOB = _args()
    {"render": render, "encode": encode, "probe": probe}[JOB["mode"]](JOB)
