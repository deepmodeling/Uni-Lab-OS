"""所有 JSON 动作一个标准：注册表按签名生成统一 schema，各处按它校验参数与返回值。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.registry import Registry
from unilabos.registry.typed_contract import TypedActionValueError, validate_action_values

SOURCE = '''
from __future__ import annotations

import enum
from typing import Any, Callable, Dict, List, Optional, TypedDict, Union

from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import PLACEHOLDER_NODES, DeviceSlot, ResourceSlot


class Mode(enum.Enum):
    FAST = "fast"
    SLOW = "slow"


class Reading(TypedDict):
    value: float
    raw: Any
    history: List[Optional[float]]


@device(id="standard_driver", category=["test"])
class Driver:
    @action()
    def untyped(self, anything, whatever: Any, label: str = None, plate: ResourceSlot = None):
        pass

    @action()
    def inferred(
        self,
        mode: Mode,
        either: Union[int, str],
        maybe: Optional[List[Optional[float]]] = None,
        callback: Callable[[int], int] = None,
        options: Dict[str, Any] = None,
        device: DeviceSlot = None,
    ) -> Reading:
        pass

    @action()
    def flexible(self, volume: float, *args, **kwargs) -> None:
        pass

    @action()
    def returns_bool(self) -> bool:
        return True

    @action()
    def returns_dict(self) -> Dict[str, Any]:
        return {}

    @action()
    def returns_bytes(self) -> bytes:
        return b""

    @action(placeholder_keys={"target": PLACEHOLDER_NODES})
    def selector(self, target: ResourceSlot) -> dict:
        return {}

    def auto_method(self, count: int, note: str = "") -> int:
        return count
'''


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    from unilabos.registry import action_schema

    monkeypatch.setattr(action_schema, "_warned", set())


@pytest.fixture()
def actions(tmp_path: Path, monkeypatch):
    # 推断按导入表解析枚举等类型：让测试模块可导入
    monkeypatch.syspath_prepend(str(tmp_path))
    path = tmp_path / "standard_driver.py"
    path.write_text(SOURCE, encoding="utf-8")
    devices, _, _ = _parse_file(path, tmp_path)
    entry = Registry()._build_device_entry_from_ast("standard_driver", devices[0])
    return entry["class"]["action_value_mappings"]


def _goal(action):
    return action["schema"]["properties"]["goal"]


def test_every_json_action_carries_the_same_schema_envelope(actions) -> None:
    for name, action in actions.items():
        if not str(action.get("type", "")).startswith("UniLabJsonCommand"):
            continue
        contract = action["schema"]["x-unilabos-action-contract"]
        assert contract["version"] == 2, name
        assert contract["input_order"] == list(_goal(action)["properties"]), name
    # 没加 @action 的公开方法同样按这个标准
    auto = actions["auto-auto_method"]
    assert _goal(auto)["properties"] == {"count": {"type": "integer"}, "note": {"type": "string", "default": ""}}
    assert _goal(auto)["required"] == ["count"]
    assert auto["schema"]["properties"]["result"] == {"type": "integer"}


def test_untyped_and_any_accept_any_value_and_none_defaults_are_nullable(actions) -> None:
    action = actions["untyped"]
    goal = _goal(action)
    assert goal["properties"]["anything"] == {}
    assert goal["properties"]["whatever"] == {}
    assert goal["properties"]["label"] == {"type": ["string", "null"], "default": None}
    assert goal["properties"]["plate"]["type"] == ["object", "null"]
    assert goal["required"] == ["anything", "whatever"]
    # 没有默认值的参数必填：缺了报清楚的错，不再补 0 / 空串
    with pytest.raises(TypedActionValueError, match="缺少必填字段：anything、whatever"):
        validate_action_values(action["schema"], {})
    values = validate_action_values(action["schema"], {"anything": [1, "x"], "whatever": {"a": None}})
    assert values == {"anything": [1, "x"], "whatever": {"a": None}, "label": None, "plate": None}
    # 可空的物料参数留空模板 {} 等于没给；不可空的物料参数给空值报错
    assert validate_action_values(action["schema"], {"anything": 1, "whatever": 1, "plate": {}})["plate"] == {}
    selector = actions["selector"]["schema"]
    with pytest.raises(TypedActionValueError, match="物料参数 target 没有给物料"):
        validate_action_values(selector, {"target": {}})
    validate_action_values(selector, {"target": [{"uuid": "x", "children": []}]})
    validate_action_values(selector, {"target": {"name": "bench_deck"}})
    with pytest.raises(TypedActionValueError, match="label"):
        validate_action_values(action["schema"], {"anything": 1, "whatever": 1, "label": 3})
    with pytest.raises(TypedActionValueError):
        validate_action_values(action["schema"], {"anything": 1, "whatever": 1, "unexpected": 1})
    # 没写返回注解：不校验返回值
    assert action["schema"]["properties"]["result"] == {}
    assert validate_action_values(action["schema"], "随便什么", section="result") == "随便什么"


def test_unknown_annotations_fall_back_to_signature_inference(actions) -> None:
    action = actions["inferred"]
    goal = _goal(action)["properties"]
    assert goal["mode"] == {"type": "string", "enum": ["fast", "slow"]}
    assert goal["either"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    assert goal["maybe"]["type"] == ["array", "null"]
    assert goal["maybe"]["items"] == {"type": ["number", "null"]}
    assert goal["callback"] == {"default": None}
    assert goal["options"] == {"type": ["object", "null"], "default": None}
    assert goal["device"]["type"] == ["string", "null"]
    assert action["placeholder_keys"] == {"device": "unilabos_devices"}
    result = action["schema"]["properties"]["result"]
    assert result["required"] == ["value", "raw", "history"]
    assert result["properties"]["raw"] == {}
    assert result["properties"]["history"] == {"type": "array", "items": {"type": ["number", "null"]}}
    assert action["schema"]["x-unilabos-action-contract"]["output_order"] == ["value", "raw", "history"]
    validate_action_values(action["schema"], {"value": 1.0, "raw": object.__name__, "history": [None, 1]}, section="result")
    with pytest.raises(TypedActionValueError, match="返回值不符合返回注解"):
        validate_action_values(action["schema"], {"value": 1.0}, section="result")


def test_kwargs_allow_extra_fields_and_args_are_ignored_with_one_warning(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="unilabos.registry.action_schema")
    path = tmp_path / "standard_driver.py"
    path.write_text(SOURCE, encoding="utf-8")
    _parse_file(path, tmp_path)
    devices, _, _ = _parse_file(path, tmp_path)
    warnings = [r.getMessage() for r in caplog.records if "*args" in r.getMessage()]
    assert len(warnings) == 1 and "Driver.flexible" in warnings[0]
    action = Registry()._build_device_entry_from_ast("standard_driver", devices[0])["class"]["action_value_mappings"][
        "flexible"
    ]
    assert _goal(action)["additionalProperties"] is True
    assert validate_action_values(action["schema"], {"volume": 1, "extra": "ok"}) == {"volume": 1, "extra": "ok"}
    # 返回注解 None：只接受空返回
    validate_action_values(action["schema"], None, section="result")
    with pytest.raises(TypedActionValueError):
        validate_action_values(action["schema"], {"done": True}, section="result")


def test_return_annotations_are_validated(actions) -> None:
    returns_bool = actions["returns_bool"]["schema"]
    assert returns_bool["properties"]["result"] == {"type": "boolean"}
    validate_action_values(returns_bool, True, section="result")
    with pytest.raises(TypedActionValueError):
        validate_action_values(returns_bool, {"success": True}, section="result")
    returns_dict = actions["returns_dict"]["schema"]
    assert returns_dict["properties"]["result"] == {"type": "object"}
    validate_action_values(returns_dict, {"anything": 1}, section="result")
    with pytest.raises(TypedActionValueError):
        validate_action_values(returns_dict, [1], section="result")
    # bytes 之类的线上形态取决于序列化：推断成接受任意值
    assert actions["returns_bytes"]["schema"]["properties"]["result"] == {}
    # test_mode 下 Host 模拟的返回值不来自驱动，不按返回注解校验
    validate_action_values(returns_bool, {"test_mode": True, "action_name": "returns_bool"}, section="result")


def test_handwritten_placeholder_changes_only_the_frontend_selector(actions) -> None:
    action = actions["selector"]
    # 类型推出 unilabos_resources，手写 unilabos_nodes：同一参数以手写为准，但仍是物料参数、照常加锁与校验
    assert action["placeholder_keys"] == {"target": "unilabos_nodes"}
    assert action["materials_need_lock"] == ["target"]
    assert _goal(action)["properties"]["target"]["x-unilabos-material-lock"] is True
