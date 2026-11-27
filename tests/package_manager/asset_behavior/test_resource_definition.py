"""从耗材资产生成物料定义（PLR + @resource 工厂）。"""
import importlib.util
import json

import pytest

pytest.importorskip("pxr")
pytest.importorskip("pylabrobot")

from pxr import Usd, UsdGeom  # noqa: E402

from unilabos.package_manager.package_distribution.asset_behavior import generate_resource_definition  # noqa: E402
from unilabos.package_manager.package_distribution.asset_measure import resource_facts  # noqa: E402
from unilabos.registry.ast_registry_scanner import _parse_file  # noqa: E402


def _box(stage, path, low, high):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    xform = UsdGeom.Xformable(cube)
    xform.AddTranslateOp().Set(tuple((low[i] + high[i]) / 2 for i in range(3)))
    xform.AddScaleOp().Set(tuple(high[i] - low[i] for i in range(3)))
    cube.CreateExtentAttr([(-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)])


def _package(tmp_path, name, sites, build):
    package = tmp_path / name / "simready_usd"
    package.mkdir(parents=True)
    stage = Usd.Stage.CreateNew(str(package / "asset.usda"))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/Asset").GetPrim())
    build(stage)
    stage.GetRootLayer().Save()
    (package / "unilab.json").write_text(json.dumps({
        "asset_id": name + "-id", "entrypoints": {"usd": "simready_usd/asset.usda"},
        "interfaces_data": {"units": "mm", "sites": sites}}), encoding="utf8")
    return package.parent


def _load(tmp_path, name, source):
    path = tmp_path / (name + ".py")
    path.write_text(source, encoding="utf8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def test_plate_definition_takes_wells_from_the_asset_sites(tmp_path):
    # 资产原点在板中心：包围盒 x ∈ [-60, 60] mm，y ∈ [-40, 40] mm，高 40 mm
    package = _package(tmp_path, "plate", [{"name": "A1", "position_mm": [-50.0, 30.0, 40.0]},
                                           {"name": "B1", "position_mm": [-50.0, 21.0, 40.0]}],
                       lambda stage: _box(stage, "/Asset/Body", (-0.06, -0.04, 0.0), (0.06, 0.04, 0.04)))
    with pytest.raises(ValueError):  # 孔深必须给并写依据
        resource_facts(package, kind="plate", well_opening=8.0)
    with pytest.raises(ValueError):
        resource_facts(package, kind="plate", well_depth="38", well_opening=8.0)
    facts = resource_facts(package, kind="plate", well_depth="38:CAD 截面", well_opening=8.0)
    assert facts["size_mm"] == pytest.approx([120.0, 80.0, 40.0]) and facts["asset_origin_mm"] == pytest.approx([60, 40, 0])
    path, module = _load(tmp_path, "generated_plate", generate_resource_definition("demo_plate", facts))
    _, resources = _parse_file(path, tmp_path)  # OS 的静态扫描能认出这个 @resource
    assert [item["resource_id"] for item in resources] == ["demo_plate"]
    assert len(module.make_demo_plate("in_graph").children) == 0  # 进设备图的实例不带孔
    plate = module.make_demo_plate("p", with_items=True)
    well = plate.get_item("A1")
    assert (well.get_size_z(), well.material_z_thickness) == (38.0, 0.0)
    center = well.get_absolute_location("c", "c", "t")
    assert (center.x, center.y, center.z) == pytest.approx((10.0, 70.0, 40.0))  # 资产 (-50, 30, 40) + 原点 (60, 40, 0)
    assert "CAD 截面" in path.read_text(encoding="utf8")


def test_plate_adapter_and_lid_definitions_have_no_items(tmp_path):
    """板的底座和盖子（压框）是物料，但没有孔位：外形从几何量，孔格和扣合深度由人给并写依据。"""
    from pylabrobot.resources import Lid, PlateAdapter

    # 底座：资产原点在中心，包围盒 x ∈ [-64, 64]，y ∈ [-43, 43]，高 20 mm；孔格第一格角点 (-53.5, -36, 5)
    base = _package(tmp_path, "base", [], lambda stage: _box(stage, "/Asset/Body", (-0.064, -0.043, 0.0), (0.064, 0.043, 0.02)))
    with pytest.raises(ValueError, match="hole_grid"):
        resource_facts(base, kind="plate_adapter")
    with pytest.raises(ValueError, match="hole_grid"):  # 没写依据
        resource_facts(base, kind="plate_adapter", hole_grid={"corner_mm": [-53.5, -36, 5], "size_mm": [8, 8], "pitch_mm": [9, 9]})
    facts = resource_facts(base, kind="plate_adapter", plate_z_offset="1.5:板裙边垫高，估计",
                           hole_grid={"corner_mm": [-53.5, -36.0, 5.0], "size_mm": [8.0, 8.0], "pitch_mm": [9.0, 9.0],
                                      "basis": "候选：按 SBS 96 孔格估计，未实测"})
    assert facts["size_mm"] == pytest.approx([128.0, 86.0, 20.0]) and facts["items"] == []
    source = generate_resource_definition("demo_base", facts, model={"format": "usdc", "entry": "models/demo_base/usd/底座.usdc"})
    path, module = _load(tmp_path, "generated_base", source)
    _, resources = _parse_file(path, tmp_path)
    assert [item["resource_id"] for item in resources] == ["demo_base"]
    adapter = module.make_demo_base("b")
    assert isinstance(adapter, PlateAdapter) and adapter.category == "plate_adapter" and not adapter.children
    assert (adapter.dx, adapter.dy, adapter.dz) == pytest.approx((10.5, 7.0, 5.0))  # 角点换到物料坐标系（最小角点为原点）
    assert (adapter.adapter_hole_size_x, adapter.adapter_hole_dx, adapter.plate_z_offset) == (8.0, 9.0, 1.5)
    assert "候选：按 SBS 96 孔格估计" in source and '"position": [0.064, 0.0, -0.043]' in source
    assert module.make_demo_base("b", with_items=True).dx == adapter.dx   # 工厂签名与其它物料定义一致

    frame = _package(tmp_path, "frame", [], lambda stage: _box(stage, "/Asset/Body", (0.0, 0.0, 0.0), (0.128, 0.086, 0.012)))
    with pytest.raises(ValueError, match="nesting_z_height"):
        resource_facts(frame, kind="lid")
    facts = resource_facts(frame, kind="lid", nesting_z_height="8:压框下沿到板顶面，CAD 量")
    source = generate_resource_definition("demo_frame", facts)
    path, module = _load(tmp_path, "generated_frame", source)
    lid = module.make_demo_frame("f")
    assert isinstance(lid, Lid) and lid.nesting_z_height == 8.0 and lid.get_size_z() == pytest.approx(12.0)
    assert [item["resource_id"] for item in _parse_file(path, tmp_path)[1]] == ["demo_frame"]


def test_tip_rack_definition_measures_the_tips_in_the_asset(tmp_path):
    def build(stage):
        _box(stage, "/Asset/Box", (-0.06, -0.04, 0.0), (0.06, 0.04, 0.07))
        UsdGeom.Xform.Define(stage, "/Asset/Tips")
        _box(stage, "/Asset/Tips/Tip_000", (-0.053, 0.027, 0.02), (-0.047, 0.033, 0.076))

    package = _package(tmp_path, "tips", [{"name": "tip_00", "position_mm": [-50.0, 30.0, 70.0], "hole_diameter_mm": 7.0}],
                       build)
    with pytest.raises(ValueError):  # 配合深度量不出来，必须给并写依据
        resource_facts(package, kind="tip_rack", tip_volume_ul=200.0)
    facts = resource_facts(package, kind="tip_rack", fitting_depth="6:估计，未标定", tip_volume_ul=200.0, tip_has_filter=True)
    assert facts["tip_length_mm"] == pytest.approx(56.0) and facts["size_mm"] == pytest.approx([120.0, 80.0, 70.0])
    _, module = _load(tmp_path, "generated_tips", generate_resource_definition("demo_tips", facts))
    rack = module.make_demo_tips("r", with_items=True)
    assert module.ITEM_IDENTIFIERS == {"tip_00": "A1"}
    spot = rack.get_item("A1")
    assert spot.name == "r_tip_00"
    tip = spot.make_tip()
    length = getattr(tip, "total_tip_length", None)  # 旧版 PLR；新版里吸头长度是 size_z
    assert (tip.get_size_z() if length is None else length, tip.fitting_depth, tip.has_filter) == (56.0, 6.0, True)
    assert facts["tip_diameter_mm"] == pytest.approx(6.0)
    top = spot.get_absolute_location("c", "c", "t")
    assert (top.x, top.y, top.z) == pytest.approx((10.0, 70.0, 76.0))  # 顶面 = 吸头顶面，高出插板 6 mm


def test_generator_rejects_bad_identifiers_and_duplicate_items():
    facts = {"kind": "plate", "asset_id": "x", "size_mm": [1, 1, 1], "asset_origin_mm": [0, 0, 0],
             "items": [("A1", 0, 0, 1, 1, 1, 1), ("A1", 0, 0, 1, 1, 1, 1)]}
    with pytest.raises(ValueError):
        generate_resource_definition("demo", facts)
    with pytest.raises(ValueError):
        generate_resource_definition("Demo-Plate", dict(facts, items=[("A1", 0, 0, 1, 1, 1, 1)]))


def test_reservoir_takes_depth_and_rectangular_opening_from_the_asset_sites(tmp_path):
    """储液槽这类库位自己声明了腔内最低点和矩形开口：不需要调用方再给孔深。"""
    package = _package(tmp_path, "trough", [
        {"name": "channel_01", "position_mm": [14.0, 40.0, 40.0], "opening_mm": [8.0, 70.0], "lowest_internal_z_mm": 3.0},
        {"name": "channel_02", "position_mm": [23.0, 40.0, 40.0], "opening_mm": [8.0, 70.0], "lowest_internal_z_mm": 3.0}],
        lambda stage: _box(stage, "/Asset/Body", (0.0, 0.0, 0.0), (0.12, 0.08, 0.04)))
    facts = resource_facts(package, kind="plate")
    _, module = _load(tmp_path, "generated_trough", generate_resource_definition("demo_trough", facts))
    assert module.ITEM_IDENTIFIERS == {"channel_01": "A1", "channel_02": "A2"}
    well = module.make_demo_trough("t", with_items=True).get_item("A2")
    assert well.name == "t_channel_02"
    assert (well.get_size_x(), well.get_size_y(), well.get_size_z()) == (8.0, 70.0, 37.0)
    bottom = well.get_absolute_location("c", "c", "b")
    assert (bottom.x, bottom.y, bottom.z) == pytest.approx((23.0, 40.0, 3.0))


def test_asset_site_names_map_to_plr_grid_without_changing_names_or_positions(tmp_path):
    """资产名与 PLR 编号分开；非标准名称按坐标编号，不能由资产表的行顺序决定。"""
    items = [("bottom_right", 20, 10, 5, 2, 2, 4), ("top_left", 10, 20, 5, 2, 2, 4),
             ("B1", 10, 10, 5, 2, 2, 4), ("top_right", 20, 20, 5, 2, 2, 4)]
    facts = {"kind": "plate", "asset_id": "grid", "size_mm": [30, 30, 5],
             "asset_origin_mm": [0, 0, 0], "items": items}
    _, module = _load(tmp_path, "generated_named_grid", generate_resource_definition("demo_named_grid", facts))
    assert module.ITEM_IDENTIFIERS == {"bottom_right": "B2", "top_left": "A1", "B1": "B1", "top_right": "A2"}
    plate = module.make_demo_named_grid("p", with_items=True)
    for label, x, y, top, *_ in items:
        well = plate.get_item(module.ITEM_IDENTIFIERS[label])
        assert well.name == f"p_{label}"
        center = well.get_absolute_location("c", "c", "t")
        assert (center.x, center.y, center.z) == (x, y, top)
    assert not module.make_demo_named_grid("p").children


def test_generated_plr_grid_supports_more_than_26_rows(tmp_path):
    facts = {"kind": "plate", "asset_id": "long_grid", "size_mm": [10, 300, 5],
             "asset_origin_mm": [0, 0, 0],
             "items": [(f"site_{i}", 5, 290 - i * 10, 5, 2, 2, 4) for i in range(28)]}
    _, module = _load(tmp_path, "generated_long_grid", generate_resource_definition("demo_long_grid", facts))
    plate = module.make_demo_long_grid("p", with_items=True)
    assert plate.get_item("AA1").name == "p_site_26"
    assert plate.get_item("AB1").name == "p_site_27"


@pytest.mark.parametrize("items", [
    [("A1", 20, 10, 5, 2, 2, 4), ("channel", 10, 10, 5, 2, 2, 4)],
    [("lower", 10, 10, 5, 2, 2, 4), ("upper", 10, 10, 10, 2, 2, 4)],
])
def test_generator_rejects_ambiguous_plr_identifiers(items):
    facts = {"kind": "plate", "asset_id": "ambiguous", "size_mm": [30, 30, 10],
             "asset_origin_mm": [0, 0, 0], "items": items}
    with pytest.raises(ValueError, match="PLR 孔位编号重复"):
        generate_resource_definition("demo_ambiguous", facts)


def test_two_packages_may_register_the_same_asset_generated_definition():
    """几个设备包按各自的资产锁生成同一个耗材的物料定义：资产 ID 和包摘要相同就不算重复。"""
    from unilabos.registry.decorators import resource

    meta = {"asset_id": "a", "package_digest": "d", "generated_from_asset": True}
    resource(id="demo_shared_generated", category=["labware"], metadata=meta)(lambda name="x": None)
    resource(id="demo_shared_generated", category=["labware"], metadata=dict(meta))(lambda name="x": None)
    with pytest.raises(ValueError):
        resource(id="demo_shared_generated", category=["labware"], metadata={**meta, "package_digest": "other"})(lambda name="x": None)
    with pytest.raises(ValueError):
        resource(id="demo_shared_generated", category=["labware"], metadata={"asset_id": "a"})(lambda name="x": None)


def test_the_display_model_is_declared_on_the_material_definition():
    """耗材是物料：三维模型挂在 @resource 上，不另建显示用的设备类。"""
    import ast

    facts = {"kind": "plate", "asset_id": "a", "asset_origin_mm": [0, 0, 0], "size_mm": [10, 10, 5],
             "items": [("A1", 5, 5, 5, 2, 2, 4)], "evidence": {}}
    source = generate_resource_definition("demo_plate_model", facts,
                                          model={"format": "usdc", "entry": "models/demo_plate_model/usd/板__R1.usdc"})
    decorators = [node.decorator_list[0] for node in ast.parse(source).body
                  if isinstance(node, ast.FunctionDef) and node.name == "make_demo_plate_model"]
    declared = {keyword.arg: ast.literal_eval(keyword.value) for keyword in decorators[0].keywords
                if keyword.arg in {"id", "model"}}
    assert declared == {"id": "demo_plate_model", "model": {"format": "usdc", "entry": "models/demo_plate_model/usd/板__R1.usdc"}}
    centred = generate_resource_definition("demo_plate_centred", {**facts, "asset_origin_mm": [63.9, 42.7, 0.0]},
                                           model={"format": "usdc", "entry": "models/demo_plate_centred/usd/板__R1.usdc"})
    assert '"position": [0.0639, 0.0, -0.0427]' in centred
    assert "model=" not in generate_resource_definition("demo_plate_plain", facts).split("@resource", 1)[1].split("\n", 1)[0]
    with pytest.raises(ValueError, match="model 必须是"):
        generate_resource_definition("demo_plate_bad", facts, model={"format": "usdc"})


def test_wells_recorded_on_the_model_become_the_interface_sites(tmp_path):
    """板类资产把孔位写在模型的库位 prim 上：搬进接口数据后，孔深由孔口高度减腔内最低点得到。"""
    from unilabos.package_manager.package_distribution.asset_behavior import facts_from_asset
    from unilabos.package_manager.package_distribution.asset_measure import geometry_site_interfaces, measure_material

    def build(stage):
        _box(stage, "/Asset/Body", (0, 0, 0), (0.02, 0.012, 0.0144))
        UsdGeom.Scope.Define(stage, "/Asset/Sites")
        for index, (name, x, y) in enumerate((("A1", 5.0, 8.0), ("A2", 9.5, 8.0), ("B1", 5.0, 3.5))):
            prim = UsdGeom.Xform.Define(stage, f"/Asset/Sites/site_{index:04d}").GetPrim()
            prim.SetCustomDataByKey("unilab", {"geometry_site_json": json.dumps(
                {"name": name, "position_mm": [x, y, 2.9], "opening_z_mm": 14.4, "depth_mm": 11.5})})
        UsdGeom.Xform.Define(stage, "/Asset/Sites/unrelated")          # 没有孔位数据的库位不算

    package = _package(tmp_path, "plate384", [], build)
    interfaces = geometry_site_interfaces(package)
    assert [site["name"] for site in interfaces["sites"]] == ["A1", "A2", "B1"]
    assert interfaces["sites"][0] == {"name": "A1", "normal": [0, 0, 1], "position_mm": [5.0, 8.0, 14.4],
                                      "lowest_internal_z_mm": 2.9}
    adapter_path = package / "simready_usd/unilab.json"
    adapter = json.loads(adapter_path.read_text(encoding="utf8"))
    adapter["interfaces_data"] = interfaces
    adapter_path.write_text(json.dumps(adapter), encoding="utf8")
    block = measure_material(package, resource_id="demo_384", kind="plate", well_opening=3.7)
    assert "well_depth" not in block["inputs"]                         # 库位自己给了腔内最低点，不用另给孔深
    adapter["parameters"] = {"material_definition": block}
    facts = facts_from_asset(adapter, "plate384")
    assert [item[-1] for item in facts["items"]] == [11.5, 11.5, 11.5]
    assert facts["items"][0][:4] == ("A1", 5.0, 8.0, 14.4)
    # 模型里没有孔位数据、或孔位缺名字时报错。
    empty = _package(tmp_path, "empty", [], lambda stage: UsdGeom.Scope.Define(stage, "/Asset/Sites"))
    with pytest.raises(ValueError, match="没有带 geometry_site_json"):
        geometry_site_interfaces(empty)
