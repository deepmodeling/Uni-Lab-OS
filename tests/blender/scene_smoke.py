"""Blender 内运行：验证 Scene 的 pose / attach / release / parent 读回。

用法（本地）：
  blender --background --factory-startup --python tests/blender/scene_smoke.py
成功时打印 BLENDER_SCENE_OK 与读回值；失败时以非零退出。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from unilabos.simulation.blender_kinematic import Scene  # noqa: E402


def identity():
    return [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def main():
    scene = Scene()
    scene.create("base", identity(), size=(0.1, 0.1, 0.1))
    scene.create("child", identity(), size=(0.05, 0.05, 0.05))

    scene.attach("child", "base")
    assert scene.parent("child") == "base", "attach must set real Blender parent"

    moved = identity()
    moved[0][3] = 0.2
    scene.set_pose("base", moved)
    scene.update()
    pose = scene.pose("child")
    assert abs(pose[0][3] - 0.2) < 1e-6, f"child must follow base, got {pose[0][3]}"

    scene.release("child")
    assert scene.parent("child") is None, "release must clear parent"

    print("BLENDER_SCENE_OK", json.dumps({"child_x": pose[0][3]}))


main()
