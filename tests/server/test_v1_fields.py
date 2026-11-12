"""dev ↔ V1 字段工具：毫秒时间戳 ↔ ISO 8601 UTC 毫秒，声明式字段改名。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from unilabos.server.api.v1_fields import FieldMap, iso_to_ms, ms_to_iso


def test_ms_to_iso_is_utc_with_milliseconds_and_z() -> None:
    assert ms_to_iso(0) == "1970-01-01T00:00:00.000Z"
    assert ms_to_iso(1_794_470_400_000) == "2026-11-12T08:00:00.000Z"
    assert ms_to_iso(1_794_470_400_999) == "2026-11-12T08:00:00.999Z"
    assert ms_to_iso(-1) == "1969-12-31T23:59:59.999Z"
    assert ms_to_iso(None) is None
    with pytest.raises(TypeError):
        ms_to_iso(True)


def test_iso_to_ms_accepts_z_offsets_and_naive_utc() -> None:
    assert iso_to_ms("2026-11-12T08:00:00.000Z") == 1_794_470_400_000
    assert iso_to_ms("2026-11-12T16:00:00.123+08:00") == 1_794_470_400_123
    assert iso_to_ms("2026-11-12T08:00:00") == 1_794_470_400_000
    # 微秒向下取整到毫秒
    assert iso_to_ms("2026-11-12T08:00:00.999999Z") == 1_794_470_400_999
    assert iso_to_ms(None) is None and iso_to_ms("  ") is None
    with pytest.raises(ValueError):
        iso_to_ms("yesterday")


@pytest.mark.parametrize("value", [0, 1, 999, 1_794_470_400_123, 4_102_444_800_000])
def test_round_trip(value: int) -> None:
    assert iso_to_ms(ms_to_iso(value)) == value


_SITE = FieldMap(rename={"site_uuid": "uuid"}, times={"updated_at_ms": "update_time"})
_MATERIAL = FieldMap(
    rename={"material_uuid": "uuid", "parent_material_uuid": "parent_uuid", "template_uuid": "resource_template_uuid"},
    times={"created_at_ms": "create_time"},
    drop={"state_hash"},
    nested={"sites": _SITE},
)


def test_field_map_renames_converts_drops_and_recurses() -> None:
    record = {
        "material_uuid": "m1",
        "parent_material_uuid": None,
        "template_uuid": "t1",
        "created_at_ms": 1_794_470_400_000,
        "state_hash": "internal",
        "name": "plate",
        "sites": [{"site_uuid": "s1", "updated_at_ms": 0, "label": "A1"}],
    }
    v1 = _MATERIAL.to_v1(record)
    assert v1 == {
        "uuid": "m1",
        "parent_uuid": None,
        "resource_template_uuid": "t1",
        "create_time": "2026-11-12T08:00:00.000Z",
        "name": "plate",
        "sites": [{"uuid": "s1", "update_time": "1970-01-01T00:00:00.000Z", "label": "A1"}],
    }
    assert _MATERIAL.to_v1([record, record]) == [v1, v1]
    assert _MATERIAL.to_v1(None) is None
    # 反向（请求体）：改回 dev 名、ISO → 毫秒，丢弃字段不会补回
    assert _MATERIAL.to_dev(v1) == {key: value for key, value in record.items() if key != "state_hash"}


def test_field_map_accepts_pydantic_models_and_strict_mode() -> None:
    class Record(BaseModel):
        material_uuid: str
        extra: int = 1

    assert _MATERIAL.to_v1(Record(material_uuid="m1")) == {"uuid": "m1", "extra": 1}
    strict = FieldMap(rename={"material_uuid": "uuid"}, keep_unmapped=False)
    assert strict.to_v1({"material_uuid": "m1", "extra": 1}) == {"uuid": "m1"}
    assert strict.to_dev({"uuid": "m1", "extra": 1}) == {"material_uuid": "m1"}
    with pytest.raises(TypeError):
        _MATERIAL.to_v1("not a dto")


def test_field_map_rejects_ambiguous_rules() -> None:
    with pytest.raises(ValueError):
        FieldMap(rename={"a": "uuid", "b": "uuid"})
    with pytest.raises(ValueError):
        FieldMap(rename={"a": "x"}, times={"a": "y"})
