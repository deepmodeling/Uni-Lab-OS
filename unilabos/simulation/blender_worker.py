"""Blender 自带 Python 的隔离入口。仅使用标准库和 bpy，不导入 OS 控制服务。"""

import json
import sys
import time

import bpy
from mathutils import Matrix, Vector


class Scene:
    def __init__(self):
        self.entities = {}
        self.libraries = {}
        self.revision = 0
        self.session = None
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)

    def create(self, entity):
        asset = entity["asset"]
        if asset["file"] not in self.libraries:
            with bpy.data.libraries.load(asset["file"], link=False) as (source, target):
                names = tuple(source.objects)
                target.objects = list(names)
            self.libraries[asset["file"]] = dict(zip(names, target.objects))
        loaded = self.libraries[asset["file"]]
        if asset["root"] not in loaded:
            raise ValueError("资产缺少显式根: " + asset["root"])
        prototype = loaded[asset["root"]]
        if prototype.parent is not None:
            raise ValueError("声明的资产根不是根对象")
        owned = {prototype, *prototype.children_recursive}
        clones = {original: original.copy() for original in owned}
        mapping = {name: clones[obj] for name, obj in loaded.items() if obj in owned}
        for original, obj in clones.items():
            obj.parent = clones.get(original.parent)
            bpy.context.scene.collection.objects.link(obj)
        for original_name, obj in mapping.items():
            obj.name = entity["uuid"] + "/" + original_name
        root = clones[prototype]
        bpy.context.view_layer.update()
        for name in [ch["object"] for ch in asset["channels"]] + asset["hidden"] + list(asset.get("probes", {}).values()):
            if name not in mapping:
                raise ValueError("绑定对象缺失: " + name)
        for name in asset["hidden"]:
            mapping[name].hide_render = True
            mapping[name].hide_set(True)
        for image in bpy.data.images:
            if image.source == "FILE" and not image.packed_file and image.users:
                import os

                path = os.path.realpath(bpy.path.abspath(image.filepath))
                if path not in asset["dependency_files"]:
                    raise ValueError("外部贴图不在摘要清单: " + path)
                if not os.path.isfile(path):
                    raise ValueError("外部贴图缺失: " + image.filepath)
        return {
            "generation": entity["generation"],
            "root": root,
            "objects": mapping,
            "bases": {name: obj.location.copy() for name, obj in mapping.items()},
            "world_bases": {name: obj.matrix_world.copy() for name, obj in mapping.items()},
            "values": {},
            "asset": asset,
            "applied": {},
        }

    def remove(self, uuid):
        state = self.entities.pop(uuid)
        for obj in list(state["objects"].values()):
            bpy.data.objects.remove(obj, do_unlink=True)

    def apply(self, frame):
        if frame["revision"] <= self.revision:
            raise ValueError("旧显示帧")
        started = time.monotonic()
        wanted = {entity["uuid"]: entity for entity in frame["entities"]}
        for uuid, state in list(self.entities.items()):
            if (
                uuid not in wanted
                or state["generation"] != wanted[uuid]["generation"]
                or state["asset"] != wanted[uuid]["asset"]
            ):
                self.remove(uuid)
        for uuid, entity in wanted.items():
            if uuid not in self.entities:
                self.entities[uuid] = self.create(entity)
        readback = {}
        for uuid, entity in wanted.items():
            state = self.entities[uuid]
            state["root"]["os_parent_uuid"] = entity["parent_uuid"] or ""
            diagnostics = []
            world_matrices = []
            for index, channel in enumerate(entity["channels"]):
                obj = state["objects"][channel["object"]]
                obj["twin_quality"] = ",".join(channel["qualities"])
                if channel["apply"]:
                    values = channel["values"]
                    if channel["kind"] == "world_matrix":
                        world_matrices.append((obj, Matrix([values[i : i + 4] for i in range(0, 16, 4)])))
                    elif channel["kind"] in ("translation", "world_translation"):
                        state["values"][index] = values[0]
                    elif channel["kind"] == "rotation":
                        obj.rotation_mode = "AXIS_ANGLE"
                        obj.rotation_axis_angle = (values[0] + channel["offset"], *channel["axis"])
                    state["applied"][index] = True
                diagnostics.append(
                    {"object": channel["object"], "applied": channel["apply"], "qualities": channel["qualities"]}
                )
            translations = {}
            for index, channel in enumerate(entity["channels"]):
                if index not in state["values"]:
                    continue
                key = (channel["object"], channel["kind"])
                translations.setdefault(key, Vector((0, 0, 0)))
                translations[key] += Vector(channel["axis"]) * (state["values"][index] + channel["offset"])
            for (name, kind), delta in translations.items():
                if kind == "translation":
                    state["objects"][name].location = state["bases"][name] + delta
                else:
                    matrix = state["world_bases"][name].copy()
                    matrix.translation += delta
                    state["objects"][name].matrix_world = matrix
            # 多个父子刚体同时使用世界矩阵时，先确认父级的求值结果。
            # 否则 Blender 按旧父矩阵计算子级局部矩阵，最终读回会漂移。
            def depth(obj):
                level = 0
                while obj.parent is not None:
                    obj = obj.parent
                    level += 1
                return level

            previous_depth = None
            for obj, matrix in sorted(world_matrices, key=lambda item: depth(item[0])):
                level = depth(obj)
                if level != previous_depth:
                    bpy.context.view_layer.update()
                    previous_depth = level
                obj.matrix_world = matrix
            readback[uuid] = {"generation": entity["generation"], "channels": diagnostics}
        bpy.context.view_layer.update()
        for uuid, entity in wanted.items():
            state = self.entities[uuid]
            # 独立读回机构末端；不得把参考 TCP 写入对象来伪造运动学一致。
            readback[uuid]["probes"] = {
                name: [float(state["objects"][obj].matrix_world[i][j]) for i in range(4) for j in range(4)]
                for name, obj in state["asset"].get("probes", {}).items()
            }
            for index, channel in enumerate(entity["channels"]):
                obj = state["objects"][channel["object"]]
                if channel["kind"] == "world_matrix":
                    actual = [float(obj.matrix_world[i][j]) for i in range(4) for j in range(4)]
                elif channel["kind"] == "world_translation":
                    actual = [
                        float(
                            (obj.matrix_world.translation - state["world_bases"][channel["object"]].translation).dot(
                                Vector(channel["axis"])
                            )
                        )
                        - channel["offset"]
                    ]
                elif channel["kind"] == "translation":
                    actual = [
                        float((obj.location - state["bases"][channel["object"]]).dot(Vector(channel["axis"])))
                        - channel["offset"]
                    ]
                else:
                    actual = [float(obj.rotation_axis_angle[0]) - channel["offset"]]
                readback[uuid]["channels"][index]["readback"] = actual if state["applied"].get(index) else None
        self.revision, self.session = frame["revision"], frame["runtime_session"]
        return {
            "revision": self.revision,
            "cursor": frame["cursor"],
            "runtime_session": self.session,
            "entities": readback,
            "apply_seconds": time.monotonic() - started,
        }

    def render(self, path, samples=64):
        if type(samples) is not int or not 1 <= samples <= 512:
            raise ValueError("渲染采样数须为1到512的整数")
        points = [
            obj.matrix_world @ Vector(corner)
            for state in self.entities.values()
            if state["asset"].get("frame_camera", True)
            for obj in state["objects"].values()
            if obj.type == "MESH" and not obj.hide_render
            for corner in obj.bound_box
        ]
        if not points:
            raise ValueError("场景无可见几何")
        low = Vector([min(p[i] for p in points) for i in range(3)])
        high = Vector([max(p[i] for p in points) for i in range(3)])
        center, span = (low + high) / 2, max(high - low)
        camera = bpy.context.scene.camera
        if camera is None:
            camera = bpy.data.objects.new("TwinCamera", bpy.data.cameras.new("TwinCamera"))
            bpy.context.scene.collection.objects.link(camera)
            bpy.context.scene.camera = camera
            lamp = bpy.data.objects.new("TwinLight", bpy.data.lights.new("TwinLight", "SUN"))
            bpy.context.scene.collection.objects.link(lamp)
            lamp.rotation_euler = (0.4, -0.6, -0.3)
            lamp.data.energy = 3
        camera.location = center + Vector((1.3, -1.7, 1.2)) * span
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        camera.data.type, camera.data.ortho_scale = "ORTHO", span * 1.4
        scene = bpy.context.scene
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = samples
        scene.render.resolution_x, scene.render.resolution_y = 960, 720
        scene.render.resolution_percentage = 100
        # 按相机实际投影拟合边界，兼顾竖向机构和画面宽高比。
        from bpy_extras.object_utils import world_to_camera_view

        bpy.context.view_layer.update()
        projected = [world_to_camera_view(scene, camera, point) for point in points]
        extent = max(max(abs(point.x - 0.5), abs(point.y - 0.5)) for point in projected)
        camera.data.ortho_scale *= max(1.0, extent / 0.45)
        bpy.context.view_layer.update()
        projected = [world_to_camera_view(scene, camera, point) for point in points]
        bounds = [[min(point[i] for point in projected), max(point[i] for point in projected)] for i in (0, 1)]
        if any(low < 0.049 or high > 0.951 for low, high in bounds):
            raise ValueError("自动取景未覆盖可见几何")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        return {"image": path, "framing_bounds": bounds}


def main():
    scene = Scene()
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            operation = request["operation"]
            if operation == "apply":
                result = scene.apply(request["frame"])
            elif operation == "render":
                result = scene.render(request["path"], request.get("samples", 64))
            elif operation == "save":
                bpy.ops.wm.save_as_mainfile(filepath=request["path"])
                result = {"file": request["path"]}
            else:
                raise ValueError("不支持的显示操作")
            response = {"id": request["id"], "ok": True, "result": result}
        except Exception as error:
            response = {"id": request.get("id"), "ok": False, "error": str(error)}
        print("UNILAB_TWIN=" + json.dumps(response, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
