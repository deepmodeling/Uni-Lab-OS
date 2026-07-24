"""P6 / P6.1 / P6.1.1 `labware_mapping.py` 单元测试 —— 对应 06-labware-mapping-table.md §11.7.7 / §11.8.7。

这些用例只依赖 `unilabos.workflow.labware_mapping` 自身与 PyYAML，
不需要 ROS2 / matplotlib / networkx 等环境，可直接 `pytest tests/workflow/test_labware_mapping.py`。

P6.1.1 schema（v1.9）：
- 顶层 key 两段：``kinds`` / ``target_devices``（**P6.1.1 起顶层 `slot_remap` 已不支持**，下沉到 ``target_devices.<device>`` 内）
- ``target_devices.default`` 是固定段名，作为兜底物料集，第一版按 prcxi 拷贝填充，**不支持 `models` 子段**
- ``target_devices.<device>.models.<model>`` 是可选的型号粒度覆盖（slot_remap / rules）
- 旧 schema（顶层 ``vendors`` / ``slot_remap`` 或 rule 含 ``prcxi_class``）会触发 warning + fallback 到 builtin
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from unilabos.workflow import labware_mapping as lm


@pytest.fixture(autouse=True)
def _reset_lru_cache():
    """每个用例后清缓存，避免 monkeypatch 跨用例污染。"""
    yield
    lm.reload_mapping()


# ==================== slot_remap ====================


@pytest.mark.parametrize(
    "raw,object_type,want",
    [
        ("4", "", "13"),
        ("8", "", "14"),
        ("12", "trash", "16"),
        ("12", "source", "12"),
        ("1", "", "1"),
        ("", "", ""),
        (4, "", "13"),  # 非字符串入参也应规整
    ],
)
def test_remap_slot_basic(raw, object_type, want):
    assert lm.remap_slot(raw, object_type) == want


def test_remap_slot_none_returns_empty():
    assert lm.remap_slot(None) == ""


def test_remap_slot_passthrough_unknown():
    assert lm.remap_slot("99") == "99"


# ==================== infer_kind ====================


def test_infer_kind_trash_priority():
    """`trash` 在 kinds 列表第 1 条 → 优先于含 'rack' 的字符串。"""
    assert lm.infer_kind("foo_trash_bar") == "trash"
    assert lm.infer_kind("opentrons_fixed_trash") == "trash"


def test_infer_kind_tiprack_before_tuberack():
    """`tiprack` 子串包含 'rack'，但应被 tip_rack 规则先抓到（顺序敏感）。"""
    assert lm.infer_kind("opentrons_96_tiprack_300ul") == "tip_rack"
    assert lm.infer_kind("opentrons_96_tiprack_20ul") == "tip_rack"


def test_infer_kind_tube_rack_variants():
    assert (
        lm.infer_kind("opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap")
        == "tube_rack"
    )
    assert lm.infer_kind("opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical") == "tube_rack"


def test_infer_kind_object_overrides_string():
    """object 字段优先：即使字符串看起来像 plate，trash / tiprack 也能强制归类。"""
    assert lm.infer_kind("anything_at_all", "tiprack") == "tip_rack"
    assert lm.infer_kind("opentrons_96_wellplate", "trash") == "trash"


def test_infer_kind_default_plate():
    assert lm.infer_kind("opentrons_96_wellplate_300ul_pcr") == "plate"
    assert lm.infer_kind("custom_384_wellplate_2200ul") == "plate"


def test_infer_kind_rack_without_tip_is_tube_rack():
    """复现历史 `_infer_reagent_kind` 中「含 rack 不含 tip → tube_rack」的语义。"""
    assert lm.infer_kind("nest_4x6_rack") == "tube_rack"


def test_infer_kind_empty_hint_returns_plate():
    assert lm.infer_kind("") == "plate"
    assert lm.infer_kind(None) == "plate"  # type: ignore[arg-type]


# ==================== resolve_target_class（target_device="prcxi"） ====================


@pytest.mark.parametrize(
    "vol,want",
    [
        (1, "PRCXI_10uL_Tips"),
        (9, "PRCXI_10uL_Tips"),
        (10, "PRCXI_10uL_Tips"),  # 闭区间 ≤10
        (11, "PRCXI_300ul_Tips"),
        (200, "PRCXI_300ul_Tips"),
        (299.9, "PRCXI_300ul_Tips"),
        (300, "PRCXI_1000uL_Tips"),  # 300 上一档（与 <300 半开等价）
        (500, "PRCXI_1000uL_Tips"),
        (1000, "PRCXI_1000uL_Tips"),
    ],
)
def test_resolve_tip_volume_buckets(vol, want):
    assert lm.resolve_target_class("prcxi", "tip_rack", 96, vol) == want


def test_resolve_tube_rack_holes():
    assert lm.resolve_target_class("prcxi", "tube_rack", 24, None) == "PRCXI_EP_Adapter"
    assert lm.resolve_target_class("prcxi", "tube_rack", 10, None) == "PRCXI_EP_Adapter"


def test_resolve_plate_holes():
    assert lm.resolve_target_class("prcxi", "plate", 96, None) == "PRCXI_BioER_96_wellplate"
    assert (
        lm.resolve_target_class("prcxi", "plate", 384, None) == "PRCXI_BioRad_384_wellplate"
    )


def test_resolve_plate_unknown_holes_returns_none():
    """48 孔板未在 YAML 列出 → None；交给 PRCXI 模板打分匹配 fallback。"""
    assert lm.resolve_target_class("prcxi", "plate", 48, 2200) is None


def test_resolve_trash_any():
    assert lm.resolve_target_class("prcxi", "trash", None, None) == "PRCXI_trash"
    # trash 规则未约束 hole_count / volume，所以任意值都命中
    assert lm.resolve_target_class("prcxi", "trash", 0, 0) == "PRCXI_trash"


# ==================== YAML 缺失 / 热加载 ====================


def test_missing_yaml_uses_builtin(monkeypatch, tmp_path):
    """YAML 文件不存在时，应自动落到 `_BUILTIN_DEFAULT`，且打 warning。"""
    bogus = tmp_path / "no_such_labware_mapping.yaml"
    monkeypatch.setattr(lm, "_DEFAULT_PATH", bogus)
    lm._load_mapping.cache_clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert lm.remap_slot("4") == "13"
        assert (
            lm.resolve_target_class("prcxi", "plate", 96, None)
            == "PRCXI_BioER_96_wellplate"
        )
    assert any("labware_mapping.yaml 未找到" in str(w.message) for w in caught)


def test_invalid_yaml_uses_builtin(monkeypatch, tmp_path):
    """YAML 解析失败也应回退到 builtin，且打 warning。"""
    bad = tmp_path / "labware_mapping.yaml"
    bad.write_text("this is :: not valid: yaml: [unclosed", encoding="utf-8")
    monkeypatch.setattr(lm, "_DEFAULT_PATH", bad)
    lm._load_mapping.cache_clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert lm.remap_slot("4") == "13"
    assert any(
        "labware_mapping.yaml 解析失败" in str(w.message)
        or "labware_mapping.yaml 根不是 dict" in str(w.message)
        for w in caught
    )


def test_yaml_reload_after_edit(monkeypatch, tmp_path):
    """临时 YAML 覆盖 + reload_mapping → 新规则生效，且原规则失效（P6.1.1 schema）。"""
    tmp_yaml = tmp_path / "labware_mapping.yaml"
    tmp_yaml.write_text(
        'kinds:\n'
        "  - { pattern: 'trash', kind: trash }\n"
        "  - { pattern: '.*',    kind: plate }\n"
        'target_devices:\n'
        '  default:\n'
        '    slot_remap:\n'
        '      default: {"4": "99"}\n'
        '      by_object: {}\n'
        '    rules:\n'
        "      - { kind: plate, hole_count: 96, class_name: PRCXI_FooPlate }\n"
        '  prcxi:\n'
        '    slot_remap:\n'
        '      default: {"4": "99"}\n'
        '      by_object: {}\n'
        '    rules:\n'
        "      - { kind: plate, hole_count: 96, class_name: PRCXI_FooPlate }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", tmp_yaml)
    lm.reload_mapping()
    assert lm.remap_slot("4") == "99"
    assert lm.resolve_target_class("prcxi", "plate", 96, None) == "PRCXI_FooPlate"
    # 新表里只有 96，没有 384 → None
    assert lm.resolve_target_class("prcxi", "plate", 384, None) is None
    # tube_rack / tip_rack 在新表里没规则 → None
    assert lm.resolve_target_class("prcxi", "tip_rack", 96, 200) is None


def test_missing_section_uses_builtin(monkeypatch, tmp_path):
    """YAML 缺 `kinds` 段 → 该段使用 builtin，其它段保留用户值（P6.1.1 schema）。"""
    partial = tmp_path / "labware_mapping.yaml"
    partial.write_text(
        'target_devices:\n'
        '  default:\n'
        '    slot_remap:\n'
        '      default: {"4": "88"}\n'
        '      by_object: {}\n'
        '    rules: []\n'
        '  prcxi:\n'
        '    slot_remap:\n'
        '      default: {"4": "88"}\n'
        '      by_object: {}\n'
        '    rules: []\n',  # 故意没有 kinds 段
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", partial)
    lm._load_mapping.cache_clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # slot_remap 用 YAML 中的覆盖值
        assert lm.remap_slot("4") == "88"
        # kinds 段缺失 → 使用 builtin 的 tiprack 规则
        assert lm.infer_kind("opentrons_96_tiprack_300ul") == "tip_rack"
    assert any("缺少 `kinds` 段" in str(w.message) for w in caught)


# ==================== P6.1 新增用例 ====================


def test_resolve_target_class_prcxi_tip_buckets():
    """PRCXI tip 量程档：≤10 / <300 / 否则 1000（与历史 _tip_prcxi_class_for_max_ul 等价）。"""
    assert lm.resolve_target_class("prcxi", "tip_rack", 96, 10) == "PRCXI_10uL_Tips"
    assert lm.resolve_target_class("prcxi", "tip_rack", 96, 200) == "PRCXI_300ul_Tips"
    assert lm.resolve_target_class("prcxi", "tip_rack", 96, 1000) == "PRCXI_1000uL_Tips"


def test_resolve_target_class_unknown_device_falls_back_to_default_section():
    """未声明的 target_device 自动回退到固定段 target_devices.default，打 warning。
    第一版 default 段内容按 prcxi 拷贝 → 断言：caller 传 'tecan' 时，结果应等于查 default 段。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # tecan / beckman / 任意未声明名字 → 全部回退到固定段 "default"
        assert (
            lm.resolve_target_class("tecan", "tip_rack", 96, 200)
            == lm.resolve_target_class("default", "tip_rack", 96, 200)
            == "PRCXI_300ul_Tips"   # 第一版 default 段按 prcxi 填，所以值仍是 PRCXI_*
        )
        assert (
            lm.resolve_target_class("unknown_xxx", "plate", 96, None)
            == lm.resolve_target_class("default", "plate", 96, None)
        )
    # 至少打 1 次 warning，提示「未声明、已回退到 default 段」
    assert any(
        ("未在 labware_mapping.yaml" in str(w.message))
        or ("target_devices.default" in str(w.message))
        for w in caught
    )


def test_resolve_target_class_per_device_tip_buckets(tmp_path, monkeypatch):
    """**P6.1 核心断言**：不同 target_device 在同一体积下命中不同 tip 量程档（P6.1.1 schema）。"""
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {}, by_object: {}}\n'
        '    rules:\n'
        '      - {kind: tip_rack, hole_count: 96, volume_max: 10,    class_name: PRCXI_10uL_Tips}\n'
        '      - {kind: tip_rack, hole_count: 96, volume_max: 299.9, class_name: PRCXI_300ul_Tips}\n'
        '      - {kind: tip_rack, hole_count: 96,                    class_name: PRCXI_1000uL_Tips}\n'
        '  prcxi:\n'
        '    slot_remap: {default: {}, by_object: {}}\n'
        '    rules:\n'
        '      - {kind: tip_rack, hole_count: 96, volume_max: 10,    class_name: PRCXI_10uL_Tips}\n'
        '      - {kind: tip_rack, hole_count: 96, volume_max: 299.9, class_name: PRCXI_300ul_Tips}\n'
        '      - {kind: tip_rack, hole_count: 96,                    class_name: PRCXI_1000uL_Tips}\n'
        '  beckman:\n'
        '    slot_remap: {default: {}, by_object: {}}\n'
        '    rules:\n'
        '      - {kind: tip_rack, hole_count: 96, volume_max: 20,    class_name: Beckman_20uL_Tips}\n'
        '      - {kind: tip_rack, hole_count: 96, volume_max: 199.9, class_name: Beckman_200uL_Tips}\n'
        '      - {kind: tip_rack, hole_count: 96,                    class_name: Beckman_1000uL_Tips}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()

    # 同样的体积 200：prcxi 走 300 档、beckman 已超出 200 档 → 1000 档
    assert lm.resolve_target_class("prcxi", "tip_rack", 96, 200) == "PRCXI_300ul_Tips"
    assert lm.resolve_target_class("beckman", "tip_rack", 96, 200) == "Beckman_1000uL_Tips"
    # 同样的体积 15：prcxi 已超出 10 档 → 300 档；beckman 仍在 20 档
    assert lm.resolve_target_class("prcxi", "tip_rack", 96, 15) == "PRCXI_300ul_Tips"
    assert lm.resolve_target_class("beckman", "tip_rack", 96, 15) == "Beckman_20uL_Tips"


def test_default_section_independent_from_prcxi(tmp_path, monkeypatch):
    """default 与 prcxi 是两段独立物料集：改 default 不影响 prcxi、改 prcxi 不影响 default。

    断言：把 default 段改成 Generic_Plate96，prcxi 段保持 PRCXI_Plate96 时，
    caller 传未声明的名字回退到 default 拿 Generic_Plate96，传 prcxi 仍拿 PRCXI_Plate96。
    """
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'                                                         # ← 独立改 default 段
        '    slot_remap: {default: {}, by_object: {}}\n'
        '    rules:\n'
        '      - {kind: plate, hole_count: 96, class_name: Generic_Plate96}\n'
        '  prcxi:\n'                                                           # ← prcxi 段保持 PRCXI_*
        '    slot_remap: {default: {}, by_object: {}}\n'
        '    rules:\n'
        '      - {kind: plate, hole_count: 96, class_name: PRCXI_Plate96}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()

    # caller 传未声明的 tecan → 走 default 段 → Generic_*
    assert lm.resolve_target_class("tecan", "plate", 96, None) == "Generic_Plate96"
    # caller 显式传 prcxi → 走 prcxi 段 → PRCXI_*（**不**受 default 影响）
    assert lm.resolve_target_class("prcxi", "plate", 96, None) == "PRCXI_Plate96"
    # 显式传 "default" 也合法（caller 可主动选择走 default 段）
    assert lm.resolve_target_class("default", "plate", 96, None) == "Generic_Plate96"


def test_legacy_yaml_schema_rejected_with_warning(tmp_path, monkeypatch):
    """旧 schema（vendors / prcxi_class）应被拒绝 + warning + 整段 fallback 到 builtin（P6.1.1 schema）。"""
    legacy = tmp_path / "labware_mapping.yaml"
    legacy.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'vendors:\n'                                                       # ← 旧顶层 key
        '  opentrons:\n'
        '    rules:\n'
        "      - {kind: plate, hole_count: 96, prcxi_class: PRCXI_FooPlate}\n",  # ← 旧字段
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", legacy)
    lm._load_mapping.cache_clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # 整段走 builtin → 96 板还是 PRCXI_BioER_96_wellplate（**不是**用户旧 YAML 中的 PRCXI_FooPlate）
        assert lm.resolve_target_class("prcxi", "plate", 96, None) == "PRCXI_BioER_96_wellplate"
    assert any(
        ("旧 schema" in str(w.message))
        or ("vendors" in str(w.message))
        or ("prcxi_class" in str(w.message))
        for w in caught
    )


def test_resolve_target_class_unknown_kind_returns_none():
    """target_device 存在、kind 不存在 → None。"""
    assert lm.resolve_target_class("prcxi", "reservoir", 12, None) is None


# ==================== P6.1.1 新增用例（slot_remap 按 device + model 分叉） ====================


def test_remap_slot_model_level_overrides_device_level(tmp_path, monkeypatch):
    """型号级 slot_remap 优先级 > 厂商级。"""
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n'
        '  prcxi:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {trash: {"12": "16"}}}\n'
        '    rules: []\n'
        '    models:\n'
        '      "4040":\n'
        '        slot_remap: {default: {"4": "16"}, by_object: {}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()
    # device 级（不传 model）→ "13"
    assert lm.remap_slot("4", target_device="prcxi") == "13"
    # model "4040" 覆盖 → "16"
    assert lm.remap_slot("4", target_device="prcxi", target_model="4040") == "16"
    # model "9320" 未声明 → 静默 fallback 到 device 级 → "13"
    assert lm.remap_slot("4", target_device="prcxi", target_model="9320") == "13"


def test_remap_slot_model_inherits_device_when_field_missing(tmp_path, monkeypatch):
    """model 子段声明但 slot_remap 字段缺失 → 静默继承厂商级；rules 同理。"""
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {}, by_object: {}}\n'
        '    rules: []\n'
        '  prcxi:\n'
        '    slot_remap: {default: {"4": "13", "8": "14"}, by_object: {}}\n'
        '    rules: [{kind: plate, hole_count: 96, class_name: PRCXI_PlateA}]\n'
        '    models:\n'
        '      "9320":\n'
        '        rules: [{kind: plate, hole_count: 96, class_name: PRCXI_PlateB}]\n',  # 仅覆盖 rules，未声明 slot_remap
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()
    # model 9320 的 slot_remap 缺字段 → 继承 prcxi.slot_remap → "4" → "13"
    assert lm.remap_slot("4", target_device="prcxi", target_model="9320") == "13"
    # model 9320 的 rules 覆盖 → PRCXI_PlateB
    assert (
        lm.resolve_target_class("prcxi", "plate", 96, None, target_model="9320")
        == "PRCXI_PlateB"
    )
    # 不传 model → 用厂商级 rules → PRCXI_PlateA
    assert lm.resolve_target_class("prcxi", "plate", 96, None) == "PRCXI_PlateA"


def test_legacy_top_level_slot_remap_rejected(tmp_path, monkeypatch):
    """P6.1.1：顶层 slot_remap 段被视为旧 schema → warning + 整段 fallback 到 builtin。"""
    legacy = tmp_path / "labware_mapping.yaml"
    legacy.write_text(
        'slot_remap:\n'                      # ← P6.1.1 已不支持的顶层段
        '  default: {"4": "99"}\n'
        '  by_object: {}\n'
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n'
        '  prcxi:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", legacy)
    lm._load_mapping.cache_clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # 整段走 builtin → "4" 仍然 → "13"（builtin 值），**不是** YAML 顶层的 "99"
        assert lm.remap_slot("4", target_device="prcxi") == "13"
    assert any(
        ("顶层" in str(w.message) and "slot_remap" in str(w.message))
        or ("旧 schema" in str(w.message))
        for w in caught
    )


def test_remap_slot_unknown_device_falls_back_with_warning(tmp_path, monkeypatch):
    """未声明的 target_device → fallback 到 default.slot_remap + warning（与 resolve_target_class 同语义）。"""
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n'
        '  prcxi:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert lm.remap_slot("4", target_device="tecan") == "13"  # fallback 到 default
    assert any(
        ("tecan" in str(w.message)) or ("target_devices.default" in str(w.message))
        for w in caught
    )


def test_remap_slot_model_only_no_device_passthrough(tmp_path, monkeypatch):
    """caller 传 target_model 但 target_device 段不存在 → 直接走 default.slot_remap（model 名忽略）。"""
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n',                          # 没有 prcxi 段
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        # target_device "prcxi" 不存在、target_model 即使传也忽略 → 走 default
        assert lm.remap_slot("4", target_device="prcxi", target_model="9320") == "13"


def test_default_section_models_subsection_warns(tmp_path, monkeypatch):
    """target_devices.default.models 不被支持 → warning，但 default.slot_remap 仍生效。"""
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n'
        '    models:\n'                              # ← default 段不支持 models
        '      "ghost":\n'
        '        slot_remap: {default: {"4": "99"}, by_object: {}}\n'
        '  prcxi:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm._load_mapping.cache_clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # default 段的 models 被忽略 → 走 default.slot_remap → "13"（不是 "99"）
        assert lm.remap_slot("4", target_device="tecan", target_model="ghost") == "13"
    assert any(
        ("default" in str(w.message) and "models" in str(w.message))
        for w in caught
    )
