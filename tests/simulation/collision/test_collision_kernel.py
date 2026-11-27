"""碰撞内核、三级检查、实例与 profile 读取（行为与机器人领域库原检查器一致）。"""
import hashlib
import json
from collections.abc import Mapping
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("fcl")

from unilabos.simulation.collision import IDENTITY, Body, CollisionInstance, CollisionProfile, Geometry  # noqa: E402
from unilabos.simulation.collision.kernel import SceneChecker, joint_samples  # noqa: E402
from unilabos.simulation.collision.refined import RefinedChecker  # noqa: E402

I = tuple(map(tuple, np.eye(4)))


def pose(x=0, y=0, z=0):
    p = np.eye(4)
    p[:3, 3] = [x, y, z]
    return tuple(map(tuple, p))


def box(name, xyz=(0, 0, 0), size=(.1, .1, .1)):
    return Body(name, pose(*xyz), (Geometry("box", size_m=size),))


# ------------------------------------------------------------------ 单一表示的内核
def test_thin_wall_and_allowed_pair_reset():
    c = SceneChecker([box("tool", (-.2, 0, 0)), box("wall", size=(.001, 1., 1.))])
    assert not c.check({"tool": pose(-.2)}, moving_ids=["tool"])["collision"]
    assert c.check({"tool": pose()}, moving_ids=["tool"])["collision"]
    assert not c.check({"tool": pose()}, moving_ids=["tool"], allowed_pairs=[("tool", "wall")])["collision"]
    assert c.check({"tool": pose()}, moving_ids=["tool"])["collision"]


def test_sibling_attachments_are_checked():
    c = SceneChecker([box("finger"), box("payload")])
    r = c.check({"finger": I, "payload": I}, moving_ids=["finger", "payload"])
    assert r["collision"] and r["eligible_pairs"] == 1


def test_native_solid_proxy_detects_contained_triangle():
    mesh = Geometry("mesh", vertices=((-0.01, 0., 0.), (.01, 0., 0.), (0., .01, 0.)), triangles=((0, 1, 2),))
    c = SceneChecker([box("solid"), Body("thin", I, (mesh,))])
    assert c.check({"thin": I}, moving_ids=["thin"])["collision"]


def test_opening_is_preserved():
    walls = Body("rack", I, (Geometry("box", size_m=(.01, .2, .2)), Geometry("box", size_m=(.01, .2, .2))),
                 geometry_poses=(pose(-.08), pose(.08)))
    c = SceneChecker([walls, box("probe", size=(.02, .02, .02))])
    assert not c.check({"probe": I}, moving_ids=["probe"])["collision"]
    assert c.check({"probe": pose(.08)}, moving_ids=["probe"])["collision"]


def test_invalid_scope_fails_closed():
    c = SceneChecker([box("a"), box("b", (2, 0, 0))])
    for kwargs in [dict(poses={}, moving_ids=["a"]), dict(poses={"a": I}, moving_ids=["ghost"]),
                   dict(poses={"a": I}, moving_ids=["a"], allowed_pairs=[("a", "ghost")]),
                   dict(poses={"a": I}, moving_ids=["a"], allowed_pairs=[("a", "b"), ("b", "a")])]:
        with pytest.raises(ValueError):
            c.check(**kwargs)
    with pytest.raises(ValueError, match="pair"):
        c.check({"a": I}, moving_ids=["a"], allowed_pairs=["ab"])


def test_static_pairs_are_omitted_but_moving_pairs_remain():
    c = SceneChecker([box("a"), box("b"), box("tool", (3, 0, 0))])
    r = c.check({"tool": pose(3)}, moving_ids=["tool"])
    assert not r["collision"] and r["eligible_pairs"] == 2 and r["narrow_phase_calls"] == 0


def test_rotated_local_shape_and_stale_native_pose():
    local = np.eye(4)
    local[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    local[0, 3] = 1.
    obj = Body("arm", I, (Geometry("box", size_m=(.6, .02, .02)),), geometry_poses=(tuple(map(tuple, local)),))
    c = SceneChecker([obj, box("probe", size=(.01, .01, .01))])
    assert c.check({"arm": I, "probe": pose(1, .25)}, moving_ids=["arm", "probe"])["collision"]
    assert not c.check({"arm": pose(5), "probe": pose()}, moving_ids=["arm", "probe"])["collision"]
    rotated = np.eye(4)
    rotated[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    assert c.check({"arm": rotated, "probe": pose(-.25, 1)}, moving_ids=["arm", "probe"])["collision"]
    assert not c.check({"arm": rotated, "probe": pose(.25, 1.2)}, moving_ids=["arm", "probe"])["collision"]


def test_subdivision_keeps_internal_points_and_budget_fails_first():
    path = [[0., 0.], [.003, 0.], [.003, .005]]
    samples = list(joint_samples(path, max_step_rad=.002))
    assert samples[0] == path[0] and samples[-1] == path[-1] and path[1] in samples
    assert np.max(abs(np.diff(samples, axis=0))) <= .002
    with pytest.raises(ValueError):
        list(joint_samples(path, max_samples=2))
    with pytest.raises(ValueError, match="budget"):
        next(joint_samples([[-1e308], [1e308]]))


def test_clearance_reports_gap_only_within_margin():
    c = SceneChecker([box("tool", (0, 0, 0)), box("wall", (.3, 0, 0))])
    assert not c.check({"tool": I}, moving_ids=["tool"])["collision"]
    gap = c.clearance(moving_ids=["tool"], margin_m=.5)
    assert gap["min_m"] == pytest.approx(.2, abs=1e-6) and set(gap["pair"]) == {"tool", "wall"}
    assert c.clearance(moving_ids=["tool"], margin_m=.1)["min_m"] is None  # 大于 margin，不报告为 0
    assert c.clearance(moving_ids=["tool"], allowed_pairs=[("tool", "wall")], margin_m=.5)["min_m"] is None


# ------------------------------------------------------------------ 三级：外包 → 精确
class Counted(Mapping):
    def __init__(self, objects):
        self.objects = {o.object_id: o for o in objects}
        self.reads = []

    def __iter__(self):
        return iter(self.objects)

    def __len__(self):
        return len(self.objects)

    def __getitem__(self, k):
        self.reads.append(k)
        return self.objects[k]


def fixture_with_opening():
    outer = box("fixture", size=(2., 2., 1.))
    exact = Body("fixture", pose(), tuple(Geometry("box", size_m=s) for s in [(.2, 2., 1.), (.2, 2., 1.), (2., .2, 1.)]),
                 geometry_poses=(pose(-.9), pose(.9), pose(y=-.9)))
    return outer, exact


def test_outer_hit_is_refined_against_exact_opening():
    outer, exact = fixture_with_opening()
    probe = box("probe")
    source = Counted([exact, probe])
    checker = RefinedChecker([outer, probe], source)
    assert source.reads == []
    free = checker.check({"probe": pose()}, moving_ids=["probe"])
    assert not free["collision"] and free["outer_hit_pairs"] == 1 and len(source.reads) == 2
    hit = checker.check({"probe": pose(.9)}, moving_ids=["probe"])
    assert hit["collision"] and hit["new_exact_pair_caches"] == 0
    assert not checker.check({"probe": pose(.9)}, moving_ids=["probe"], allowed_pairs=[("fixture", "probe")])["collision"]


def test_exact_pose_cache_and_free_broadphase_never_loads_exact():
    a, b = box("a", size=(1., 1., 1.)), box("b")
    checker = RefinedChecker([a, b], {"a": a, "b": b})
    assert checker.check({"a": pose(), "b": pose()}, moving_ids=["b"])["collision"]
    repeat = checker.check({"b": pose()}, moving_ids=["b"])
    assert repeat["cached_exact_pose_results"] == 1 and repeat["exact_narrow_phase_calls"] == 0
    far = Counted([a, box("c", (10, 0, 0))])
    lazy = RefinedChecker([a, box("c", (10, 0, 0))], far)
    assert not lazy.check({"c": pose(10)}, moving_ids=["c"])["collision"] and far.reads == []
    with pytest.raises(ValueError, match="identities"):
        RefinedChecker([a, b], {"a": a})


def test_refined_clearance_uses_exact_geometry_inside_outer_envelope():
    outer, exact = fixture_with_opening()
    checker = RefinedChecker([outer, box("probe")], {"fixture": exact, "probe": box("probe")})
    assert not checker.check({"probe": pose()}, moving_ids=["probe"])["collision"]
    gap = checker.clearance(moving_ids=["probe"], margin_m=.5)
    # 外包与探针相交（外包距离为 0），精确的开口里离最近的壁 0.75 m 以上
    assert gap["level"] == "exact" and gap["min_m"] is None
    gap = checker.clearance(moving_ids=["probe"], margin_m=1.0)
    assert gap["min_m"] == pytest.approx(.75, abs=1e-6)


# ------------------------------------------------------------------ 实例与 profile
def test_two_instances_share_geometry_and_have_independent_world_poses():
    initial = np.eye(4)
    initial[0, 3] = .4
    original = Body("/axis", tuple(map(tuple, initial)), (Geometry("box", size_m=(.1, .1, .1)),))
    catalog = SimpleNamespace(objects={"/axis": original}, object_bodies={"/axis": "/axis"})
    placement = np.eye(4)
    placement[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    placement[1, 3] = 2.
    a = CollisionInstance(catalog, "os-instance-1", placement)["os-instance-1::/axis"]
    b = CollisionInstance(catalog, "os-instance-2", np.eye(4))["os-instance-2::/axis"]
    assert a.geometry is b.geometry
    assert np.allclose(np.array(a.world_pose)[:3, 3], [0, 2.4, 0])
    checker = SceneChecker([a, b])
    assert not checker.check({a.object_id: a.world_pose}, moving_ids=[a.object_id])["collision"]
    assert checker.check({a.object_id: b.world_pose}, moving_ids=[a.object_id])["collision"]
    scaled = np.eye(4)
    scaled[0, 0] = 2
    with pytest.raises(ValueError, match="Rigid"):
        CollisionInstance(catalog, "x", scaled)


def artifact(tmp_path):
    np.savez(tmp_path / "geometry.npz", v=np.array([[0., 0, 0], [1, 0, 0], [0, 1, 0]]), n=np.array([3]), i=np.array([0, 1, 2]))
    matrix = np.eye(4)
    matrix[0, 3] = .3
    document = dict(status="exported", units="metres", matrix_convention="column_vectors", arrays="geometry.npz",
                    arrays_sha256=hashlib.sha256((tmp_path / "geometry.npz").read_bytes()).hexdigest(),
                    bodies=[dict(path="/body", role="material", world_from_body_initial=np.eye(4).tolist(),
                                 shapes=[dict(type="Mesh", point_frame="body_local", vertices="v", face_counts="n",
                                              face_indices="i", hole_faces=[], prim="/body/mesh"),
                                         dict(type="Cube", body_from_shape=matrix.tolist(), parameters={"size": .1},
                                              prim="/body/cube")])])
    (tmp_path / "geometry.json").write_text(json.dumps(document))
    return tmp_path / "geometry.json"


def test_profile_reads_body_local_mesh_and_primitive_frames(tmp_path):
    scene = CollisionProfile(artifact(tmp_path))
    obj = scene.objects["/body"]
    assert np.frombuffer(obj.geometry[0].vertices.data, "<f8").reshape(-1, 3)[1].tolist() == [1, 0, 0]
    assert obj.geometry_poses[0] == IDENTITY and obj.geometry_poses[1][0][3] == .3
    assert obj.geometry[1].size_m == (.1, .1, .1)
    assert SceneChecker([obj, box("probe", (.3, 0, 0))]).check({"/body": I}, moving_ids=["/body"])["collision"]


def test_profile_lazy_matches_eager_and_tampering_fails_closed(tmp_path):
    path = artifact(tmp_path)
    data = json.loads(path.read_text())
    data["bodies"][0]["shapes"][0]["collision_object_id"] = "/body/contact"
    data["bodies"][0]["shapes"][1]["collision_object_id"] = "/body/structure"
    path.write_text(json.dumps(data))
    lazy, eager = CollisionProfile(path, lazy=True), CollisionProfile(path)
    assert set(lazy.objects) == {"/body/contact", "/body/structure"} and len(lazy.objects.cache) == 0
    assert lazy.objects["/body/contact"] == eager.objects["/body/contact"] and len(lazy.objects.cache) == 1
    (tmp_path / "geometry.npz").write_bytes(b"changed after lazy catalog creation")
    with pytest.raises(ValueError, match="changed"):
        lazy.objects["/body/structure"]
    with pytest.raises(ValueError, match="digest"):
        CollisionProfile(path)


# ------------------------------------------------------------------ 部署图 → 整站碰撞场景
def test_station_scene_places_shared_profiles_at_graph_poses(tmp_path):
    from unilabos.simulation.collision.scene import node_world_poses, station_scene

    path = artifact(tmp_path)
    document = {"nodes": [
        {"id": "bench", "class": "bench", "parent": None, "children": ["left", "right"],
         "position": {"x": 1000, "y": 0, "z": 0}, "rotation": {"x": 0, "y": 0, "z": 90}},
        {"id": "left", "class": "fixture", "parent": "bench", "children": [], "position": {"x": 500, "y": 0, "z": 0}},
        {"id": "right", "class": "fixture", "parent": "bench", "children": [], "position": {"x": -500, "y": 0, "z": 0}},
    ], "scene": {"node_bindings": {"right": {"asset_transform": pose(0, 0, .1)}}}}
    world = node_world_poses(document)
    assert np.allclose(world["left"][:3, 3], [1, .5, 0]) and np.allclose(world["right"][:3, 3], [1, -.5, 0])
    scene = station_scene(document, {"left": (path, path), "right": (path, path)})
    assert set(scene.instances) == {"left", "right"} and len(scene.outer_objects) == 2
    right = next(o for o in scene.outer_objects if o.object_id == "right::/body")
    assert np.allclose(np.asarray(right.world_pose)[:3, 3], [1, -.5, .1])   # asset_transform 在节点之后应用一次
    checker = scene.checker()
    probe_at_left = np.asarray(world["left"]) @ np.asarray(pose(.3, 0, 0))
    assert checker.check({"left::/body": probe_at_left}, moving_ids=["left::/body"])["collision"] is False
    with pytest.raises(ValueError, match="不在部署图"):
        station_scene(document, {"ghost": (path, path)})
