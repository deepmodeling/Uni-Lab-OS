"""dev 存储字段 ↔ V1.0 对外字段的小工具（API 层 DTO 转换用）。

存储保持 dev（``*_at_ms`` 毫秒时间戳、``material_uuid`` 等列名），对外按 V1 合同输出：

- 时间：ISO 8601 UTC 毫秒，``2026-11-12T08:00:00.000Z``（:func:`ms_to_iso` / :func:`iso_to_ms`）；
- 字段名：用 :class:`FieldMap` 声明一次 dev → V1 的改名 / 时间转换 / 丢弃 / 嵌套规则，
  响应用 :meth:`FieldMap.to_v1`，请求体用 :meth:`FieldMap.to_dev` 反向换回。

例::

    MATERIAL = FieldMap(
        rename={"material_uuid": "uuid", "parent_material_uuid": "parent_uuid"},
        times={"created_at_ms": "create_time", "updated_at_ms": "update_time"},
        drop={"internal_hash"},
        nested={"children": CHILD},
    )
    MATERIAL.to_v1(record)          # 单个对象，或对象列表
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ms_to_iso(value: Optional[int | float]) -> Optional[str]:
    """毫秒时间戳 → ``YYYY-MM-DDTHH:MM:SS.mmmZ``（UTC）；``None`` 原样返回。

    用整数毫秒做 timedelta，不经浮点秒，避免 ``.999`` 之类的舍入误差。
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("时间戳不能是布尔值")
    moment = _EPOCH + timedelta(milliseconds=int(value))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def iso_to_ms(value: Optional[str]) -> Optional[int]:
    """ISO 8601 时间 → 毫秒时间戳（向下取整到毫秒）；``None`` / 空串返回 ``None``。

    接受 ``Z`` 或 ``±HH:MM`` 时区后缀；不带时区的按 UTC 解释（V1 合同里所有时间都是 UTC）。

    Raises:
        ValueError: 不是合法的 ISO 8601 时间。
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    moment = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


@dataclass(frozen=True)
class FieldMap:
    """一个 DTO 的 dev → V1 字段规则（只描述差异，其余字段原样透传）。

    Attributes:
        rename: dev 字段名 → V1 字段名。
        times: dev 毫秒时间戳字段 → V1 ISO 时间字段（同时改名并换算）。
        drop: 对外不输出的 dev 内部字段（:meth:`to_dev` 时也不会凭空补回）。
        nested: 字段（dev 名）→ 子对象 / 子对象列表套用的 :class:`FieldMap`。
        keep_unmapped: ``False`` 时只输出规则里出现过的字段。
    """

    rename: Mapping[str, str] = field(default_factory=dict)
    times: Mapping[str, str] = field(default_factory=dict)
    drop: Iterable[str] = field(default_factory=frozenset)
    nested: Mapping[str, "FieldMap"] = field(default_factory=dict)
    keep_unmapped: bool = True

    def __post_init__(self) -> None:
        targets = list(self.rename.values()) + list(self.times.values())
        duplicated = {name for name in targets if targets.count(name) > 1}
        if duplicated:
            raise ValueError(f"多个 dev 字段映射到同一个 V1 字段：{sorted(duplicated)}")
        overlap = set(self.rename) & set(self.times)
        if overlap:
            raise ValueError(f"字段既改名又按时间换算：{sorted(overlap)}")
        object.__setattr__(self, "drop", frozenset(self.drop))

    # ── dev → V1 ─────────────────────────────────────────────────

    def to_v1(self, value: Any) -> Any:
        """dev 对象（dict / pydantic 模型）或其列表 → V1 字典；``None`` 原样返回。"""

        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return [self.to_v1(item) for item in value]
        source = _as_mapping(value)
        result: dict[str, Any] = {}
        for key, item in source.items():
            if key in self.drop:
                continue
            if key in self.nested:
                item = self.nested[key].to_v1(item)
            if key in self.times:
                result[self.times[key]] = ms_to_iso(item)
            elif key in self.rename:
                result[self.rename[key]] = item
            elif self.keep_unmapped:
                result[key] = item
        return result

    # ── V1 → dev（请求体） ───────────────────────────────────────

    def to_dev(self, value: Any) -> Any:
        """V1 请求体（或其列表）→ dev 字段名与毫秒时间戳；``None`` 原样返回。"""

        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return [self.to_dev(item) for item in value]
        source = _as_mapping(value)
        renamed = {v1: dev for dev, v1 in self.rename.items()}
        timed = {v1: dev for dev, v1 in self.times.items()}
        result: dict[str, Any] = {}
        for key, item in source.items():
            if key in timed:
                dev_key = timed[key]
                item = iso_to_ms(item)
            elif key in renamed:
                dev_key = renamed[key]
            elif self.keep_unmapped:
                dev_key = key
            else:
                continue
            if dev_key in self.drop:
                continue
            if dev_key in self.nested:
                item = self.nested[dev_key].to_dev(item)
            result[dev_key] = item
        return result


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="python")
    raise TypeError(f"不支持转换的 DTO 类型：{type(value).__name__}")


__all__ = ["FieldMap", "iso_to_ms", "ms_to_iso"]
