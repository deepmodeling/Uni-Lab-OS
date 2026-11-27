"""显示消费的身份、未知值和资产边界；实际 Blender 由独立集成运行验证。"""

import hashlib
from pathlib import Path

import pytest

from unilabos.runtime.observations import Descriptor, Property, StateError
from unilabos.simulation.display import Channel, DisplayAsset, SceneMirror


def inputs(tmp_path, semantic="observed"):
    asset_path = tmp_path / "model.blend"
    asset_path.write_bytes(b"fixture-only")
    asset = DisplayAsset(
        version="1",
        file="model.blend",
        sha256=hashlib.sha256(asset_path.read_bytes()).hexdigest(),
        root="root",
        channels=[Channel(object="axis", kind="translation", properties=["q"])],
    )
    descriptor = Descriptor(version="1", properties={"q": Property(semantic=semantic, unit="m", frame="local")})
    node = dict(
        uuid="native-id",
        generation="1",
        parent_uuid=None,
        resource_template_uuid="template",
        descriptor=descriptor.model_dump(),
        properties={"q": {"quality": "unknown", "value": None}},
    )
    return asset, dict(runtime_session="runtime", graph_version="graph", cursor=1, nodes=[node])


def test_unknown_and_stale_are_not_motion(tmp_path):
    asset, snapshot = inputs(tmp_path)
    mirror = SceneMirror({"template": asset}, tmp_path)
    assert not mirror.frame(snapshot)["entities"][0]["channels"][0]["apply"]
    snapshot["nodes"][0]["properties"]["q"] = {"quality": "good", "value": 0.1}
    assert mirror.frame(snapshot)["entities"][0]["channels"][0]["values"] == [0.1]
    snapshot["nodes"][0]["properties"]["q"]["quality"] = "stale"
    assert not mirror.frame(snapshot)["entities"][0]["channels"][0]["apply"]
    snapshot["nodes"] = []
    assert mirror.frame(snapshot)["entities"] == []


def test_display_cannot_substitute_goal(tmp_path):
    asset, snapshot = inputs(tmp_path, semantic="goal")
    with pytest.raises(StateError, match="display_requires_observation"):
        SceneMirror({"template": asset}, tmp_path).frame(snapshot)


def test_model_digest_and_scope_are_checked(tmp_path):
    asset, _ = inputs(tmp_path)
    (tmp_path / "model.blend").write_bytes(b"changed")
    with pytest.raises(StateError, match="model.blend"):
        SceneMirror({"template": asset}, tmp_path)
    outside = tmp_path.parent / "outside.blend"
    outside.write_bytes(b"outside")
    with pytest.raises(StateError, match="asset_path_outside_root"):
        asset.model_copy(update={"file": "../outside.blend"}).validate_files(tmp_path)


def test_scene_uses_native_uuid_and_generation(tmp_path):
    asset, snapshot = inputs(tmp_path)
    snapshot["nodes"].append({**snapshot["nodes"][0], "uuid": "second", "generation": "2"})
    frame = SceneMirror({"template": asset}, tmp_path).frame(snapshot)
    assert [(e["uuid"], e["generation"]) for e in frame["entities"]] == [("native-id", "1"), ("second", "2")]
