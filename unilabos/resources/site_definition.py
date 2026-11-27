"""资源模板（ResourceTemplate）拥有的库位（Site）固定定义。"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    field_validator,
    model_validator,
)

INITIAL_MATERIAL_TEMPLATE_HINT = "initial_material_template"


class SiteDefinition(BaseModel):
    """不含实例身份和库位占用（SiteOccupancy）的库位模板定义。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    schema_version: Literal[1] = 1
    index: Union[int, str]
    label: str
    visible: bool = True
    position_x: float = 0.0
    position_y: float = 0.0
    position_z: float = 0.0
    width: float = Field(default=0.0, ge=0.0)
    length: float = Field(default=0.0, ge=0.0)
    depth: float = Field(default=0.0, ge=0.0)
    rotation_x: float = 0.0
    rotation_y: float = 0.0
    rotation_z: float = 0.0
    content_type: List[str] = Field(default_factory=list)
    allowed_resource_template_uuids: List[str] = Field(default_factory=list)
    initial_material_template_uuid: Optional[str] = None
    parent_link: str = ""
    description: str = ""
    meta_data: Dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_nested_geometry(cls, value: Any) -> Any:
        """把遗留嵌套几何转换为 Backend 使用的扁平字段。

        参数：``value`` 是装饰器、YAML 或 AST 提供的可疑库位定义。返回：不与
        调用方共享容器的扁平对象；非对象、嵌套字段类型错误或新旧字段冲突时抛出
        ``ValueError``，禁止形成含糊的资源模板（ResourceTemplate）事实。
        """

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("库位（Site）模板定义必须是对象")

        site = copy.deepcopy(value)

        def promote_xyz(source_key: str, target_prefix: str) -> None:
            """提升一个三维向量的轴字段。

            参数：``source_key`` 是旧嵌套字段名，``target_prefix`` 是新字段前缀。
            返回：无，原地更新当前分离副本；结构非法或新旧值冲突时抛出
            ``ValueError``。
            """

            nested = site.pop(source_key, None)
            if nested is None:
                return
            if not isinstance(nested, dict):
                raise ValueError(f"库位（Site）.{source_key} 必须是对象")
            for axis in ("x", "y", "z"):
                if axis not in nested:
                    continue
                target = f"{target_prefix}{axis}"
                if target in site and float(site[target]) != float(nested[axis]):
                    raise ValueError(
                        f"库位（Site）.{target} 与 {source_key}.{axis} 冲突"
                    )
                site.setdefault(target, nested[axis])

        promote_xyz("position", "position_")
        promote_xyz("rotation", "rotation_")

        size = site.pop("size", None)
        if size is not None:
            if not isinstance(size, dict):
                raise ValueError("库位（Site）.size 必须是对象")
            for old_key, target in (
                ("width", "width"),
                ("height", "length"),
                ("length", "length"),
                ("depth", "depth"),
            ):
                if old_key not in size:
                    continue
                if target in site and float(site[target]) != float(size[old_key]):
                    raise ValueError(
                        f"库位（Site）.{target} 与 size.{old_key} 冲突"
                    )
                site.setdefault(target, size[old_key])
        return site

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str) -> str:
        """规范化库位显示名称并拒绝空名称。

        参数：``value`` 是一个模板内稳定的库位名称。返回：去除首尾空白的名称；
        空值抛出 ``ValueError``。
        """

        if not isinstance(value, str) or not value.strip():
            raise ValueError("库位（Site）.label 不能为空")
        return value.strip()

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: Union[int, str]) -> Union[int, str]:
        """规范化模板作者声明的库位索引。

        参数：``value`` 是数字或字符串索引。返回：保持原类型的规范索引；布尔值
        或空字符串抛出 ``ValueError``。
        """

        if isinstance(value, bool):
            raise ValueError("库位（Site）.index 不能是布尔值")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("库位（Site）.index 不能为空")
        return value

    @field_validator("content_type", "allowed_resource_template_uuids")
    @classmethod
    def _normalize_string_list(
        cls,
        values: List[str],
        info: ValidationInfo,
    ) -> List[str]:
        """规范化库位准入字符串数组并按大小写不敏感规则去重。

        参数：``values`` 是待规范化字符串，``info`` 标识当前字段。返回：保留首次
        声明顺序的非空字符串数组；发现空项目时抛出 ``ValueError``。
        """

        result: List[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"库位（Site）.{info.field_name} 只能包含非空字符串"
                )
            normalized = value.strip()
            key = normalized.casefold()
            if key not in seen:
                result.append(normalized)
                seen.add(key)
        return result

    @field_validator("initial_material_template_uuid", mode="before")
    @classmethod
    def _validate_initial_material_template_uuid(
        cls,
        value: Any,
    ) -> Optional[str]:
        """校验库位默认内容模板身份。

        参数：``value`` 是作者声明的可选资源模板 UUID。返回：``None`` 或保持原
        拼写的规范非 nil UUID；类型、空白、大小写或连字符形式不规范时抛出
        ``ValueError``，禁止静默改写模板事实。
        """

        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                "库位（Site）.initial_material_template_uuid 必须是规范 UUID"
            )
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise ValueError(
                "库位（Site）.initial_material_template_uuid 必须是规范 UUID"
            ) from error
        canonical = str(parsed)
        if parsed.int == 0 or value != canonical:
            raise ValueError(
                "库位（Site）.initial_material_template_uuid 必须是规范 UUID"
            )
        return value

    @model_validator(mode="after")
    def _require_initial_material_template_allowed(self) -> "SiteDefinition":
        """保证默认内容 UUID 和待解析模板短名提示合法。"""

        if (
            self.initial_material_template_uuid is not None
            and self.allowed_resource_template_uuids
            and self.initial_material_template_uuid
            not in self.allowed_resource_template_uuids
        ):
            raise ValueError(
                "库位（Site）.initial_material_template_uuid "
                "必须包含在 allowed_resource_template_uuids 中"
            )
        unilab_metadata = self.meta_data.get("unilab")
        if (
            isinstance(unilab_metadata, dict)
            and INITIAL_MATERIAL_TEMPLATE_HINT in unilab_metadata
        ):
            template_name = unilab_metadata[INITIAL_MATERIAL_TEMPLATE_HINT]
            if (
                not isinstance(template_name, str)
                or not template_name.strip()
                or template_name != template_name.strip()
            ):
                raise ValueError(
                    "库位（Site）.meta_data.unilab."
                    "initial_material_template 必须是稳定模板短名"
                )
        return self


def normalize_available_sites(
    value: Optional[List[Any]],
) -> List[Dict[str, Any]]:
    """规范化资源模板顶层 ``available_sites`` 数组。

    参数：``value`` 是装饰器、YAML、AST 或模板快照边界提供的库位定义数组；
    ``None`` 表示模板不拥有库位。返回：不携带实例 UUID 或库位占用
    （SiteOccupancy）的扁平对象列表；数组形状、字段或模板内身份重复时抛出
    ``ValueError``。
    """

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("available_sites 必须是数组")

    result: List[Dict[str, Any]] = []
    seen_indexes: set[tuple[str, Union[int, str]]] = set()
    seen_labels: set[str] = set()
    for ordinal, raw_site in enumerate(value):
        if isinstance(raw_site, SiteDefinition):
            site = raw_site
        else:
            if not isinstance(raw_site, dict):
                raise ValueError(f"available_sites[{ordinal}] 必须是对象")
            payload = copy.deepcopy(raw_site)
            payload.setdefault("index", ordinal)
            payload.setdefault("label", str(payload["index"]))
            site = SiteDefinition.model_validate(payload)

        index_key = (type(site.index).__name__, site.index)
        if index_key in seen_indexes:
            raise ValueError(f"available_sites 中存在重复 index: {site.index}")
        label_key = site.label.casefold()
        if label_key in seen_labels:
            raise ValueError(f"available_sites 中存在重复 label: {site.label}")
        seen_indexes.add(index_key)
        seen_labels.add(label_key)
        result.append(site.model_dump())
    return result


__all__ = [
    "INITIAL_MATERIAL_TEMPLATE_HINT",
    "SiteDefinition",
    "normalize_available_sites",
]
