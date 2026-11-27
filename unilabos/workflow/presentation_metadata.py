"""参数表单的声明式展示元数据；不参与运行时值约束。"""

from __future__ import annotations

from typing import Any

from unilabos.workflow.json_codec import decode_json_bytes, encode_json

STRING_SUGGESTIONS_KEY = "x-unilabos-string-suggestions"
STRING_EDITOR_KEY = "x-unilabos-string-editor"
PRESENTATION_KEYS = frozenset({
    "x-unilabos-enum-labels", "x-unilabos-dependent-options", "format", "x-unilab-accept",
    STRING_SUGGESTIONS_KEY, STRING_EDITOR_KEY,
})


def parse_presentation_metadata(value: Any, *, value_kind: str | None = None) -> dict[str, Any]:
    """仅透传已知展示命名空间，保持 JSON 标量类型及空值。"""

    if type(value) is not dict or set(value) - PRESENTATION_KEYS:
        raise ValueError("参数展示元数据包含不支持的字段")
    try:
        result = decode_json_bytes(encode_json(value))
    except (TypeError, ValueError) as error:
        raise ValueError("参数展示元数据必须是 JSON") from error
    file_keys = {"format", "x-unilab-accept"}
    if file_keys & result.keys():
        if value_kind != "string" or result.get("format") != "unilab-file-ref" or result.get("x-unilab-accept") != [".xlsx"]:
            raise ValueError("托管文件标记仅支持 string/.xlsx，且必须完整声明")
    if STRING_SUGGESTIONS_KEY in result:
        suggestions = result[STRING_SUGGESTIONS_KEY]
        if (
            value_kind != "string"
            or not isinstance(suggestions, list)
            or not suggestions
            or any(type(option) is not str or not option.strip() for option in suggestions)
            or len(set(suggestions)) != len(suggestions)
        ):
            raise ValueError("字符串建议必须是非空且不重复的字符串数组")
    if STRING_EDITOR_KEY in result and (
        value_kind != "string" or result[STRING_EDITOR_KEY] != "json-csv-rows"
    ):
        raise ValueError("CSV 行编辑器仅用于字符串参数")
    for key, item in result.items():
        if key in {"format", STRING_EDITOR_KEY}:
            continue
        if not isinstance(item, list):
            raise ValueError(f"{key} 必须是数组")
    return result
