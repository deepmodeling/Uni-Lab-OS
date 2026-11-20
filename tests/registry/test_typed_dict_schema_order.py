"""TypedDict 结果的推断 schema：``required`` 按字段声明顺序，与字符串哈希种子无关。

``__required_keys__`` 是 frozenset，直接遍历时顺序随 ``PYTHONHASHSEED`` 变化，同一个动作在不同进程里
生成的返回 schema 和注册表内容哈希就会不同。
"""

from typing import NotRequired, TypedDict

from unilabos.registry.registry import _runtime_type_schema


class _Reading(TypedDict):
    wavelength_nm: float
    absorbance: float
    transmittance: float
    sample_id: str
    blank_id: NotRequired[str]
    temperature_c: float


DECLARED = ["wavelength_nm", "absorbance", "transmittance", "sample_id", "blank_id", "temperature_c"]
REQUIRED = ["wavelength_nm", "absorbance", "transmittance", "sample_id", "temperature_c"]


class _OtherOrderSet(frozenset):
    """遍历顺序与声明顺序不同的 frozenset：在一个进程里重现「换了哈希种子」的效果。"""

    def __iter__(self):
        return iter(sorted(frozenset.__iter__(self), reverse=True))


def test_required_follows_declaration_order() -> None:
    schema = _runtime_type_schema(_Reading)

    assert list(schema["properties"]) == DECLARED
    assert schema["required"] == REQUIRED


def test_required_order_does_not_follow_set_iteration(monkeypatch) -> None:
    monkeypatch.setattr(_Reading, "__required_keys__", _OtherOrderSet(_Reading.__required_keys__))
    assert list(_Reading.__required_keys__) != REQUIRED

    assert _runtime_type_schema(_Reading)["required"] == REQUIRED
