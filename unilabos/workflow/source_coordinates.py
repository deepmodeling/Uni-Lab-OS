"""工作流作者源码与前端编辑器之间的 UTF-16 坐标规则。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


def require_utf8_text(value: str) -> str:
    """确认字符串可无损编码为 UTF-8 并返回原值。

    参数说明：``value`` 是源码或源码 URI。类型错误抛出 ``TypeError``，包含
    未配对代理项等非法 Unicode 时抛出 ``ValueError``，成功时返回同一文本。
    """

    if not isinstance(value, str):
        raise TypeError("作者源码必须是字符串")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("作者源码必须是有效 UTF-8 文本") from None
    return value


def source_lines(source: str) -> tuple[str, ...]:
    """按 Python 通用换行形式拆分源码。

    参数说明：``source`` 必须是有效 UTF-8 文本。返回值保留空行但不保留换行
    符，至少含一行，供一基行号和 UTF-16 列号校验使用。
    """

    require_utf8_text(source)
    return tuple(re.split(r"\r\n|\r|\n", source))


def utf16_length(value: str) -> int:
    """返回字符串占用的 UTF-16 编码单元数量。

    参数说明：``value`` 是一段有效 Unicode 文本；返回值不包含终止符，非 BMP
    字符按两个单元计算。
    """

    require_utf8_text(value)
    return len(value.encode("utf-16-le")) // 2


def codepoint_offset_to_utf16_column(line: str, offset: int) -> int:
    """把 Python 字符偏移转换为一基 UTF-16 列号。

    参数说明：``line`` 是不含换行符的单行文本，``offset`` 是零基字符偏移，
    允许指向行尾；返回前端编辑器使用的一基列号。越界抛出 ``ValueError``。
    """

    require_utf8_text(line)
    if type(offset) is not int or not 0 <= offset <= len(line):
        raise ValueError("源码字符偏移越界")
    return utf16_length(line[:offset]) + 1


def utf8_offset_to_utf16_column(line: str, byte_offset: int) -> int:
    """把 UTF-8 字节偏移转换为一基 UTF-16 列号。

    参数说明：``line`` 是单行文本，``byte_offset`` 是零基 UTF-8 字节偏移，
    必须落在字符边界；返回一基 UTF-16 列号，否则抛出 ``ValueError``。
    """

    encoded = require_utf8_text(line).encode("utf-8")
    if type(byte_offset) is not int or not 0 <= byte_offset <= len(encoded):
        raise ValueError("源码字节偏移越界")
    try:
        prefix = encoded[:byte_offset].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("源码字节偏移没有落在字符边界") from None
    return utf16_length(prefix) + 1


def source_ranges_fit(
    source: str,
    ranges: Iterable[Mapping[str, Any]],
) -> bool:
    """判断所有一基 UTF-16 源码范围是否落在文本内。

    参数说明：``source`` 是完整作者源码，``ranges`` 中每项必须含起止行列；
    返回 ``True`` 表示所有端点有效且起点不晚于终点，任何非法结构返回
    ``False``，不向服务层泄漏解析异常。
    """

    try:
        lines = source_lines(source)
        for source_range in ranges:
            start = _position(source_range, "start")
            end = _position(source_range, "end")
            if start > end or not _position_fits(lines, start):
                return False
            if not _position_fits(lines, end):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _position(source_range: Mapping[str, Any], prefix: str) -> tuple[int, int]:
    """读取一个源码范围端点。

    参数说明：``source_range`` 是范围映射，``prefix`` 只能为 ``start`` 或
    ``end``；返回 ``(line, column)``，字段不是正整数时抛出 ``ValueError``。
    """

    if prefix not in {"start", "end"}:
        raise ValueError("源码范围端点名称无效")
    line = source_range[f"{prefix}_line"]
    column = source_range[f"{prefix}_column"]
    if type(line) is not int or type(column) is not int or line < 1 or column < 1:
        raise ValueError("源码范围端点必须是正整数")
    return line, column


def _position_fits(lines: tuple[str, ...], position: tuple[int, int]) -> bool:
    """判断一个一基 UTF-16 位置是否位于源码行内。

    参数说明：``lines`` 是已拆分源码，``position`` 是行列二元组；行尾后一列
    合法，返回布尔结果。
    """

    line, column = position
    return line <= len(lines) and column <= utf16_length(lines[line - 1]) + 1


__all__ = [
    "codepoint_offset_to_utf16_column",
    "require_utf8_text",
    "source_lines",
    "source_ranges_fit",
    "utf16_length",
    "utf8_offset_to_utf16_column",
]
