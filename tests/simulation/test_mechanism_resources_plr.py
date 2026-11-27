"""运行中的 PLR 资源树作为点位来源：与图节点来源给出同样的点，并随资源树变化。"""
import json

import pytest

pytest.importorskip("pylabrobot")

from pylabrobot.resources import Coordinate, Cor_96_wellplate_360ul_Fb, Resource  # noqa: E402
from pylabrobot.resources.hamilton import hamilton_96_tiprack_300uL_filter  # noqa: E402

from unilabos.simulation.mechanism import MechanismError  # noqa: E402
from unilabos.simulation.mechanism_resources import PlrResourcePoints, ResourcePoints  # noqa: E402


def _deck():
    deck = Resource(name="deck", size_x=1000, size_y=600, size_z=0)
    deck.assign_child_resource(hamilton_96_tiprack_300uL_filter(name="tips"), location=Coordinate(100, 60, 120))
    deck.assign_child_resource(Cor_96_wellplate_360ul_Fb(name="plate"), location=Coordinate(400, 60, 150))
    return deck


def _nodes(resource, parent=None, out=None):
    out = [] if out is None else out
    location = resource.location
    config = {"size_x": resource.get_size_x(), "size_y": resource.get_size_y(), "size_z": resource.get_size_z(),
              "material_z_thickness": getattr(resource, "material_z_thickness", None)}
    if hasattr(resource, "make_tip"):
        tip = resource.make_tip()
        config["prototype_tip"] = {"total_tip_length": tip.total_tip_length, "fitting_depth": tip.fitting_depth}
    out.append({"id": resource.name, "parent": parent, "type": "deck" if parent is None else "resource",
                "position": {axis: getattr(location, axis) if location else 0 for axis in "xyz"}, "config": config})
    for child in resource.children:
        _nodes(child, resource.name, out)
    return out


def test_plr_tree_and_graph_nodes_give_the_same_points_and_attributes():
    deck = _deck()
    live, static = PlrResourcePoints(deck), ResourcePoints(json.loads(json.dumps(_nodes(deck))))
    for name in ("plate_well_A1", "plate_well_H12", "tips_tipspot_C5"):
        for anchor in ("origin", "top_center", "bottom_center", "center"):
            assert live.point(name, anchor) == pytest.approx(static.point(name, anchor), abs=1e-9)
    for attribute in ("tip_length", "fitting_depth", "size_x"):
        assert live.attribute("tips_tipspot_A1", attribute) == pytest.approx(
            static.attribute("tips_tipspot_A1", attribute))
    with pytest.raises(MechanismError) as error:
        live.point("missing", "origin")
    assert error.value.code == "resource_unknown"


def test_plr_points_follow_the_tree_when_a_plate_is_moved():
    deck = _deck()
    live = PlrResourcePoints(deck)
    before = live.point("plate_well_A1", "bottom_center")
    plate = deck.get_resource("plate")
    plate.unassign()
    deck.assign_child_resource(plate, location=Coordinate(700, 60, 150))
    after = live.point("plate_well_A1", "bottom_center")
    assert after[0] - before[0] == pytest.approx(0.3) and after[1:] == pytest.approx(before[1:])


def _scene(tmp_path):
    import json

    package = tmp_path / "plate" / "simready_usd"
    package.mkdir(parents=True)
    (package / "unilab.json").write_text(json.dumps({"interfaces_data": {"units": "mm", "sites": [
        {"name": "A1", "normal": [0, 0, 1], "position_mm": [10.0, 20.0, 40.0], "depth_mm": 38.0},
        {"name": "tip_00", "normal": [0, 0, 1], "position_mm": [1.0, 0.0, 70.0], "top_offset_mm": 6.0,
         "tip_length_mm": 58.5, "fitting_depth_mm": 6.0},
        {"name": "bare", "normal": [0, 0, 1], "position_mm": [0.0, 0.0, 0.0]}]}}), encoding="utf8")

    def node(identifier, parent, position, yaw=0.0, world=None, asset=None):
        pose = {"position": dict(zip("xyz", position)), "rotation": {"x": 0, "y": 0, "z": yaw}}
        if world is not None:
            pose["extra"] = {"world_position_mm": dict(zip("xyz", world))}
        return {"id": identifier, "parent": parent, "pose": pose, "position": pose["position"],
                "data": {"scene": {"asset_id": asset}} if asset else {}}

    nodes = [node("handler", None, (1000.0, 2000.0, 500.0)),
             node("nest", "handler", (100.0, 0.0, 50.0)),
             node("box", "nest", (0.0, 10.0, 5.0), yaw=180.0, asset="plate-asset"),
             # 逻辑挂载：图里的相对位置是 0，真实位置在 world_position_mm
             node("plate", "handler", (0.0, 0.0, 0.0), world=(1300.0, 2100.0, 600.0), asset="plate-asset")]
    return nodes, {"plate-asset": tmp_path / "plate"}


def test_scene_points_combine_node_pose_with_the_asset_sites(tmp_path):
    import pytest

    from unilabos.simulation.mechanism import MechanismError
    from unilabos.simulation.mechanism_resources import SceneResourcePoints

    nodes, packages = _scene(tmp_path)
    points = SceneResourcePoints(nodes, "handler", packages, frame_origin_m=(0.1, 0.0, 0.0))
    # 逐级相对位置：nest(100,0,50) + box(0,10,5)，盒子转 180°，库位 (1,0,70) → (-1,0,70)；吸头顶面再高 6
    assert points.point("box/tip_00") == pytest.approx((0.099 - 0.1, 0.010, 0.131))
    assert points.point("box/tip_00", "origin") == pytest.approx((0.099 - 0.1, 0.010, 0.125))
    # world_position_mm 优先于图里的逻辑位置；孔底 = 孔口 - 声明的孔深
    assert points.point("plate/A1", "top_center") == pytest.approx((0.310 - 0.1, 0.120, 0.140))
    assert points.point("plate/A1", "bottom_center") == pytest.approx((0.310 - 0.1, 0.120, 0.102))
    assert points.attribute("box/tip_00", "tip_length") == pytest.approx(0.0585)
    assert points.attribute("box/tip_00", "fitting_depth") == pytest.approx(0.006)
    for call, code in ((lambda: points.point("plate/bare", "bottom_center"), "material_geometry_undeclared"),
                       (lambda: points.attribute("plate/A1", "tip_length"), "material_geometry_undeclared"),
                       (lambda: points.point("plate/Z9"), "resource_unknown"),
                       (lambda: points.point("nest/A1"), "resource_asset_unavailable"),
                       (lambda: points.point("plate"), "resource_unknown")):
        with pytest.raises(MechanismError) as error:
            call()
        assert error.value.code == code


def test_scene_points_place_a_material_at_the_pose_of_the_site_it_occupies(tmp_path):
    """放进库位的物料自己的位姿是零：它的原点和朝向取库位的位姿，结果与直接写在节点位姿上一样。"""
    import pytest

    from unilabos.simulation.mechanism_resources import SceneResourcePoints

    nodes, packages = _scene(tmp_path)
    free = SceneResourcePoints(nodes, "handler", packages).point("box/tip_00")
    nodes[1]["config"] = {"sites": [{"name": "pos_1", "position": {"x": 0.0, "y": 10.0, "z": 5.0},
                                     "rotation": {"x": 0, "y": 0, "z": 180.0}}]}
    nodes[2]["pose"] = {"position": {"x": 0, "y": 0, "z": 0}, "rotation": {"x": 0, "y": 0, "z": 0}}
    nodes[2]["position"] = nodes[2]["pose"]["position"]
    nodes[2]["data"]["initial_site"] = "pos_1"
    assert SceneResourcePoints(nodes, "handler", packages).point("box/tip_00") == pytest.approx(free)


def test_scene_points_reject_tilted_nodes(tmp_path):
    import pytest

    from unilabos.simulation.mechanism import MechanismError
    from unilabos.simulation.mechanism_resources import SceneResourcePoints

    nodes, packages = _scene(tmp_path)
    nodes[1]["pose"]["rotation"]["x"] = 5.0
    with pytest.raises(MechanismError) as error:
        SceneResourcePoints(nodes, "handler", packages).point("box/tip_00")
    assert error.value.code == "resource_rotation_unsupported"


def test_scene_points_expand_items_from_the_material_definition(tmp_path, monkeypatch):
    """有物料定义（PLR + @resource 工厂）的资产：孔位从物料类按需展开，设备图里的节点仍不带子节点。"""
    import pytest

    pytest.importorskip("pylabrobot")
    from unilabos.simulation.mechanism_resources import SceneResourcePoints

    (tmp_path / "generated_plate.py").write_text(
        "from collections import OrderedDict\n"
        "from pylabrobot.resources import Coordinate, Plate, Well\n"
        "ASSET_ORIGIN_MM = [60.0, 40.0, 0.0]\n"
        "def make_plate(name='p', with_items=False):\n"
        "    if not with_items:\n"
        "        return Plate(name, 120.0, 80.0, 40.0, ordering=OrderedDict())\n"
        "    well = Well('A1', size_x=8.0, size_y=8.0, size_z=38.0, material_z_thickness=0.0)\n"
        "    well.location = Coordinate(66.0, 56.0, 2.0)\n"
        "    return Plate(name, 120.0, 80.0, 40.0, ordered_items=OrderedDict(A1=well))\n", encoding="utf8")
    monkeypatch.syspath_prepend(str(tmp_path))
    nodes, _ = _scene(tmp_path)
    points = SceneResourcePoints(nodes, "handler", definitions={"plate-asset": "generated_plate:make_plate"})
    # 孔中心在物料坐标系 (70, 60)，资产原点在 (60, 40) → 资产坐标系 (10, 20)；与直接读资产库位的那组数一致
    assert points.point("plate/A1", "top_center") == pytest.approx((0.310, 0.120, 0.140))
    assert points.point("plate/A1", "bottom_center") == pytest.approx((0.310, 0.120, 0.102))


def test_tip_length_prefers_the_physical_length_over_firmware_size_z():
    from types import SimpleNamespace

    from unilabos.simulation.mechanism_resources import _tip_length

    firmware = SimpleNamespace(physical_length_mm=96.1, get_size_z=lambda: 32.6)   # 例如 Tecan 1000 微升吸头
    assert _tip_length(firmware) == 96.1
    assert _tip_length(SimpleNamespace(total_tip_length=58.5)) == 58.5
    assert _tip_length(SimpleNamespace(get_size_z=lambda: 50.4)) == 50.4
