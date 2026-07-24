"""P6 §17 hint bug —— `_infer_plate_num_children_from_labware_hint` 误把
reagent_id 末尾数字（如 ``samples_6`` 的 ``_6``）当作孔板规格，导致
``_apply_target_labware_class_auto_match`` fallback 到 PRCXI 4-孔 trough 模板。

跨板 fix（P2 v2 §14）把 plate name 作为 prefix 编码进 ``well_names`` 之后，
runtime 调用 ``plate.get_well("A5")`` 严格定位 well，trough plate 上不存在
``A5`` 会直接 IndexError，使得这个隐藏多年的孔数推断 bug 浮出。

修复策略（方案 A）
-----
hint 只用 ``item.get("labware", "")``，**不再**拼上 ``labware_id``（reagent_key
是业务名，不应参与孔板规格推断）。

测试矩阵
----
- ``test_reagent_key_numeric_suffix_must_not_match_hint`` —— samples_6 / samples_24 /
  samples_96 + nunc_rectangular_agar_plate → hint 返回 None（labware string 不带孔数信息）。
- ``test_labware_string_X_well_correctly_inferred`` —— labware="nest_96_wellplate..." → 96；
  "custom_384_wellplate" → 384；"nest_24_wellplate_2ml_pcr" → 24。
- ``test_apply_does_not_classify_samples_6_as_trough`` —— 集成：构造 Agar Plating-like
  reagent block（slot 8 上 12 个 samples_X，X 末尾含 6/24/96），跑
  ``_apply_target_labware_class_auto_match`` 后，samples_6/24 不再得到 trough class。
- ``test_real_labware_96_wellplate_still_inferred_via_labware_str`` —— 即便 labware_id
  与孔数无关，``nest_96_wellplate_100ul_pcr_full_skirt`` 这种 labware 命名仍应被识别为 96。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _install_fake_optional_deps() -> None:
    if "matplotlib" not in sys.modules:
        sys.modules["matplotlib"] = types.ModuleType("matplotlib")
    if "matplotlib.pyplot" not in sys.modules:
        sys.modules["matplotlib.pyplot"] = types.ModuleType("matplotlib.pyplot")


_install_fake_optional_deps()

import pytest  # noqa: E402

from unilabos.workflow.common import (  # noqa: E402
    _apply_target_labware_class_auto_match,
    _infer_plate_num_children_from_labware_hint,
    _reconcile_slot_carrier_target_class,
)


# ==================== unit：hint 函数本身 ====================


@pytest.mark.parametrize(
    "labware_id",
    ["samples_6", "samples_24", "samples_96", "samples_12", "samples_48"],
)
def test_reagent_key_numeric_suffix_must_not_match_hint(labware_id):
    """reagent_id 末尾的孔数关键字数字不应被识别为孔板规格。"""
    item = {
        "slot": 8,
        "well": ["A5"],
        "labware": "nunc_rectangular_agar_plate",
        "object": "target",
    }
    assert _infer_plate_num_children_from_labware_hint(labware_id, item) is None, (
        f"reagent_id {labware_id!r} 不应被识别为孔板规格 "
        f"（其末尾数字应当被忽略；labware string 不含 96/384/etc 关键字）"
    )


@pytest.mark.parametrize(
    "labware_str,expected",
    [
        ("nest_96_wellplate_100ul_pcr_full_skirt", 96),
        ("custom_384_wellplate", 384),
        ("nest_24_wellplate_2ml_pcr", 24),
        ("custom_48_wellplate", 48),
        ("opentrons_12_wellplate_15ml", 12),
        ("nest_6_wellplate_5ml", 6),
        ("nunc_rectangular_agar_plate", None),
        ("", None),
    ],
)
def test_labware_string_well_count_inferred(labware_str, expected):
    item = {"labware": labware_str}
    assert (
        _infer_plate_num_children_from_labware_hint("samples", item) == expected
    ), f"labware {labware_str!r} 应推断为 {expected!r}"


# ==================== integration：模拟 Agar Plating ====================


def _agar_plating_reagent_block():
    """反推自 unilabos_data/req_workflow_upload.json：12 列 × 9 reagent per step。

    slot 8 (mapped 14) 上 12 个 reagent_keys: samples_6, samples_15, samples_24,
    samples_33, samples_42, samples_51, samples_60, samples_69, samples_78,
    samples_87, samples_96, samples_105.
    """
    info = {}
    slot_for_idx = {0: 3, 1: 4, 2: 5, 3: 6, 4: 7, 5: 8, 6: 9, 7: 10, 8: 11}
    cols = [f"A{i + 1}" for i in range(12)]
    for col_i, col in enumerate(cols):
        for di in range(9):
            n = col_i * 9 + di + 1
            key = "samples" if n == 1 else f"samples_{n}"
            info[key] = {
                "slot": slot_for_idx[di],
                "well": [col],
                "labware": "nunc_rectangular_agar_plate",
                "object": "target",
            }
    for i in range(12):
        key = "sources" if i == 0 else f"sources_{i + 1}"
        info[key] = {
            "slot": 2,
            "well": [cols[i]],
            "labware": "nest_96_wellplate_100ul_pcr_full_skirt",
            "object": "source",
        }
    info["tiprack_1"] = {
        "slot": 1,
        "well": None,
        "labware": "opentrons_96_tiprack_10ul",
        "object": "tiprack",
    }
    info["trash"] = {
        "slot": 12,
        "well": None,
        "labware": "opentrons_1_trash_1100ml_fixed",
        "object": "trash",
    }
    return info


def test_apply_does_not_classify_samples_6_as_trough():
    """集成回归：Agar Plating-like reagent block 跑完类匹配 + slot 统一后，
    slot 8 上 12 个 reagent 不应得到 4-孔 trough class。"""
    info = _agar_plating_reagent_block()
    _apply_target_labware_class_auto_match(
        info, preserve_tip_rack_incoming_class=True, target_device="prcxi"
    )
    _reconcile_slot_carrier_target_class(
        info, preserve_tip_rack_incoming_class=True, target_device="prcxi"
    )
    slot8_keys = [
        "samples_6", "samples_15", "samples_24", "samples_33",
        "samples_42", "samples_51", "samples_60", "samples_69",
        "samples_78", "samples_87", "samples_96", "samples_105",
    ]
    for k in slot8_keys:
        cls = info[k].get("target_class_name") or ""
        assert "trough" not in cls.lower(), (
            f"reagent {k} 被误识别为 trough class: {cls!r}；"
            "这通常是 hint 误把 reagent_id 末尾数字当孔板规格"
        )


def test_real_labware_96_wellplate_still_inferred_via_labware_str():
    """labware string 含 96_wellplate 时应该正常识别为 96，不被 fix 破坏。"""
    item = {
        "slot": 2,
        "well": ["A1"],
        "labware": "nest_96_wellplate_100ul_pcr_full_skirt",
        "object": "source",
    }
    assert _infer_plate_num_children_from_labware_hint("sources", item) == 96
