"""--complete_registry 写回 yaml 时，字典类 status 归一为 str。"""

import pytest

from unilabos.registry.registry import _normalize_status_return_type


@pytest.mark.parametrize(
    "annotation",
    [
        "Dict[str, Any]",
        "Dict[MotorAxis, MotorPosition]",
        "Dict[int, Dict]",
        "dict",
        "Mapping[str, str]",
        "Optional[Dict[str, Any]]",
    ],
)
def test_dict_like_status_becomes_str(annotation: str) -> None:
    assert _normalize_status_return_type(annotation) == "str"


@pytest.mark.parametrize("annotation", ["str", "float", "bool", "List[int]", "Optional[float]"])
def test_other_status_types_are_kept(annotation: str) -> None:
    expected = "float" if annotation == "Optional[float]" else annotation
    assert _normalize_status_return_type(annotation) == expected


def test_empty_annotation_defaults_to_str() -> None:
    assert _normalize_status_return_type(None) == "str"
    assert _normalize_status_return_type("") == "str"
