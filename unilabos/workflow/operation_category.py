"""领域包内实验操作类别的稳定目录。"""

from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    assert_directory_identity,
    read_regular_path,
)
from unilabos.workflow.source_publication import (
    SourcePublicationConflict,
    SourcePublicationError,
    atomic_publish_source,
)

CATEGORY_FILE_NAME = "operation_categories.json"
CATEGORY_FILE_BYTE_LIMIT = 128 * 1024
CATEGORY_LIMIT = 200
CATEGORY_NAME_BYTE_LIMIT = 256

# 三个产品默认类别使用固定身份，而不是从可改名的显示名称计算。UUID 必须跨
# 进程、领域包首次落盘和前端旧标签兼容保持不变；删除类别只修改领域包快照，
# 不能重新生成或复用这些身份指向别的类别。
DEVICE_OPERATION_CATEGORY_UUID = "1ade6f36-40a9-58fe-a6c8-c7418e651a49"
INTEGRATED_OPERATION_CATEGORY_UUID = "10997cc6-104e-53c8-b860-1ccc4957f9a0"
MANUAL_OPERATION_CATEGORY_UUID = "1866cd8a-bf4c-5a44-85e0-325cfc7a063c"

_DEFAULT_CATEGORIES = (
    {
        "uuid": DEVICE_OPERATION_CATEGORY_UUID,
        "name": "设备操作",
        "sort_order": 10,
    },
    {
        "uuid": INTEGRATED_OPERATION_CATEGORY_UUID,
        "name": "集成操作",
        "sort_order": 20,
    },
    {
        "uuid": MANUAL_OPERATION_CATEGORY_UUID,
        "name": "人工操作",
        "sort_order": 30,
    },
)

_LEGACY_TAG_CATEGORY_UUIDS = {
    "device_operation": DEVICE_OPERATION_CATEGORY_UUID,
    "integrated_operation": INTEGRATED_OPERATION_CATEGORY_UUID,
    "integration_operation": INTEGRATED_OPERATION_CATEGORY_UUID,
    "manual_operation": MANUAL_OPERATION_CATEGORY_UUID,
}


class OperationCategoryError(RuntimeError):
    """实验操作类别输入、身份、并发或文件持久化错误。"""

    def __init__(self, code: str) -> None:
        """建立稳定分类错误。

        参数：``code`` 是服务层可映射的错误类别。返回：无。异常：无；错误对象
        本身用于关闭当前操作，且不会暴露底层文件系统细节。
        """

        self.code = code
        super().__init__(code)


def default_operation_categories() -> list[dict[str, Any]]:
    """返回三个产品默认实验操作类别。

    参数：无。返回：按默认展示顺序排列的独立字典，调用方可安全修改。异常：无。
    """

    return deepcopy(list(_DEFAULT_CATEGORIES))


def legacy_operation_category_uuid(tags: Any) -> str | None:
    """把旧版实验操作类别标签映射到稳定类别身份。

    参数：``tags`` 是旧工作流公开标签值。返回：命中首个已知类别时返回稳定
    UUID，否则返回 ``None``。异常：无；非列表或非字符串项直接忽略。
    """

    if not isinstance(tags, list):
        return None
    for value in tags:
        if isinstance(value, str) and value in _LEGACY_TAG_CATEGORY_UUIDS:
            return _LEGACY_TAG_CATEGORY_UUIDS[value]
    return None


class OperationCategoryCatalog:
    """以领域包 JSON 文件为唯一持久权威的实验操作类别目录。"""

    def __init__(
        self,
        *,
        package_root: Path,
        package_root_identity: tuple[int, int],
    ) -> None:
        """绑定一个已授权且身份冻结的领域包目录。

        参数：``package_root`` 是真实 Python 包根；``package_root_identity`` 是
        发现时设备号/索引节点。返回：无。异常：目录已被替换时首次读写关闭失败。
        """

        self._package_root = Path(package_root)
        self._package_root_identity = package_root_identity
        self._lock = threading.RLock()

    def list_categories(self) -> list[dict[str, Any]]:
        """读取当前全部实验操作类别。

        参数：无。返回：按 ``sort_order``、名称和 UUID 稳定排序的独立字典列表。
        异常：目录或配置不安全、内容损坏时抛 ``OperationCategoryError``。
        """

        with self._lock:
            categories, _expected_hash = self._read()
            return deepcopy(categories)

    def get_category(self, category_uuid: str) -> dict[str, Any]:
        """按稳定 UUID 读取一个实验操作类别。

        参数：``category_uuid`` 是类别身份。返回：独立类别字典。异常：UUID
        非法或类别不存在时分别抛 ``invalid_input``、``not_found``。
        """

        identity = _category_uuid(category_uuid)
        for category in self.list_categories():
            if category["uuid"] == identity:
                return category
        raise OperationCategoryError("not_found")

    def create_category(self, *, name: str, sort_order: int) -> dict[str, Any]:
        """新增并持久化一个实验操作类别。

        参数：``name`` 是展示名，``sort_order`` 是升序展示权重。返回：新类别。
        异常：名称重复、数量超限、并发写入或文件错误时关闭当前写入。
        """

        normalized_name = _category_name(name)
        normalized_order = _sort_order(sort_order)
        with self._lock:
            categories, expected_hash = self._read()
            if len(categories) >= CATEGORY_LIMIT:
                raise OperationCategoryError("invalid_input")
            _require_unique_name(categories, normalized_name)
            # 自定义类别在首次创建时取得一次随机稳定身份，随后随领域包快照
            # 持久化；改名和调整顺序只改属性，绝不重新生成 UUID。
            category = {
                "uuid": str(uuid4()),
                "name": normalized_name,
                "sort_order": normalized_order,
            }
            self._write([*categories, category], expected_hash=expected_hash)
            return dict(category)

    def update_category(
        self,
        category_uuid: str,
        *,
        name: str | None = None,
        sort_order: int | None = None,
    ) -> dict[str, Any]:
        """修改类别名称或展示顺序，保持稳定 UUID 不变。

        参数：``category_uuid`` 定位类别；名称和顺序至少提供一项。返回：更新后
        类别。异常：身份、名称、顺序、并发或文件状态不合法时关闭写入。
        """

        identity = _category_uuid(category_uuid)
        if name is None and sort_order is None:
            raise OperationCategoryError("invalid_input")
        normalized_name = None if name is None else _category_name(name)
        normalized_order = None if sort_order is None else _sort_order(sort_order)
        with self._lock:
            categories, expected_hash = self._read()
            existing = next(
                (item for item in categories if item["uuid"] == identity),
                None,
            )
            if existing is None:
                raise OperationCategoryError("not_found")
            if normalized_name is not None:
                _require_unique_name(
                    categories,
                    normalized_name,
                    excluding_uuid=identity,
                )
                existing["name"] = normalized_name
            if normalized_order is not None:
                existing["sort_order"] = normalized_order
            self._write(categories, expected_hash=expected_hash)
            return dict(existing)

    def delete_category(self, category_uuid: str) -> None:
        """删除一个未被工作流引用的类别定义。

        参数：``category_uuid`` 是已由服务层完成引用检查的类别身份。返回：无。
        异常：类别不存在、并发变化或文件异常时关闭删除。
        """

        identity = _category_uuid(category_uuid)
        with self._lock:
            categories, expected_hash = self._read()
            remaining = [item for item in categories if item["uuid"] != identity]
            if len(remaining) == len(categories):
                raise OperationCategoryError("not_found")
            self._write(remaining, expected_hash=expected_hash)

    def _read(self) -> tuple[list[dict[str, Any]], str | None]:
        """读取稳定配置；文件缺失时投影产品默认值。

        参数：无。返回：已排序类别和 CAS 原稿哈希，缺失文件的哈希为 ``None``。
        异常：目录身份、普通文件或 JSON 合同不可信时抛 ``unavailable``。
        """

        try:
            assert_directory_identity(
                self._package_root,
                self._package_root_identity,
            )
            snapshot = read_regular_path(
                self._package_root / CATEGORY_FILE_NAME,
                byte_limit=CATEGORY_FILE_BYTE_LIMIT,
                missing_ok=True,
            )
            if snapshot is None:
                return _sorted_categories(default_operation_categories()), None
            categories = _decode_categories(snapshot.content)
            return categories, _sha256(snapshot.content)
        except OperationCategoryError:
            raise
        except (OSError, StableFileAccessError):
            raise OperationCategoryError("unavailable") from None

    def _write(
        self,
        categories: list[dict[str, Any]],
        *,
        expected_hash: str | None,
    ) -> None:
        """以 CAS 原子发布完整类别目录快照。

        参数：``categories`` 是新全集；``expected_hash`` 是读取到的旧稿哈希，
        缺失时为 ``None``。返回：无。异常：并发变化映射 ``conflict``，I/O 或
        目录问题映射 ``unavailable``。
        """

        normalized = _validate_categories(categories)
        content = (
            json.dumps(
                {"version": 1, "categories": normalized},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        try:
            assert_directory_identity(
                self._package_root,
                self._package_root_identity,
            )
            atomic_publish_source(
                parent_path=self._package_root,
                target_name=CATEGORY_FILE_NAME,
                content=content,
                byte_limit=CATEGORY_FILE_BYTE_LIMIT,
                expected_hash=expected_hash,
            )
        except SourcePublicationConflict:
            raise OperationCategoryError("conflict") from None
        except (OSError, SourcePublicationError, StableFileAccessError):
            raise OperationCategoryError("unavailable") from None


def _decode_categories(raw: bytes) -> list[dict[str, Any]]:
    """解析封闭的 UTF-8 JSON 类别文档。

    参数：``raw`` 是受大小限制的文件内容。返回：校验并排序后的类别。异常：
    编码、重复键、非有限数字或结构错误时抛 ``unavailable``。
    """

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """把 JSON 键值对还原为对象并拒绝重复键。

        参数：``pairs`` 保留解析器读取顺序及重复项。返回：无重复键的字典。
        异常：发现重复键时抛 ``ValueError``，使整个类别快照关闭式失败。
        """

        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        MemoryError,
        RecursionError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise OperationCategoryError("unavailable") from None
    if not isinstance(document, dict) or set(document) != {"version", "categories"}:
        raise OperationCategoryError("unavailable")
    if document["version"] != 1 or not isinstance(document["categories"], list):
        raise OperationCategoryError("unavailable")
    try:
        return _validate_categories(document["categories"])
    except OperationCategoryError:
        raise OperationCategoryError("unavailable") from None


def _validate_categories(values: list[Any]) -> list[dict[str, Any]]:
    """校验类别全集的字段、身份和唯一性。

    参数：``values`` 是待验证列表。返回：规范化并排序后的新列表。异常：任一项
    非法、UUID/名称重复或超过数量上限时抛 ``invalid_input``。
    """

    if not isinstance(values, list) or len(values) > CATEGORY_LIMIT:
        raise OperationCategoryError("invalid_input")
    categories: list[dict[str, Any]] = []
    identities: set[str] = set()
    names: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"uuid", "name", "sort_order"}:
            raise OperationCategoryError("invalid_input")
        identity = _category_uuid(value["uuid"])
        name = _category_name(value["name"])
        sort_order = _sort_order(value["sort_order"])
        folded_name = name.casefold()
        if identity in identities or folded_name in names:
            raise OperationCategoryError("invalid_input")
        identities.add(identity)
        names.add(folded_name)
        categories.append({"uuid": identity, "name": name, "sort_order": sort_order})
    return _sorted_categories(categories)


def _category_uuid(value: Any) -> str:
    """规范化实验操作类别 UUID。

    参数：``value`` 是输入身份。返回：小写连字符形式的非零 UUID。异常：类型、
    格式或零 UUID 不合法时抛 ``invalid_input``。
    """

    if not isinstance(value, str):
        raise OperationCategoryError("invalid_input")
    try:
        identity = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise OperationCategoryError("invalid_input") from None
    if identity.int == 0:
        raise OperationCategoryError("invalid_input")
    return str(identity)


def _category_name(value: Any) -> str:
    """规范化非空类别展示名。

    参数：``value`` 是输入名称。返回：去除首尾空白后的名称。异常：非字符串、
    空名称、控制字符或字节长度超限时抛 ``invalid_input``。
    """

    if not isinstance(value, str):
        raise OperationCategoryError("invalid_input")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > CATEGORY_NAME_BYTE_LIMIT
        or any(ord(character) < 0x20 for character in normalized)
    ):
        raise OperationCategoryError("invalid_input")
    return normalized


def _sort_order(value: Any) -> int:
    """校验类别展示顺序。

    参数：``value`` 是候选整数。返回：0 到 10000 的原值。异常：布尔值、非整数
    或越界时抛 ``invalid_input``。
    """

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 10_000
    ):
        raise OperationCategoryError("invalid_input")
    return value


def _require_unique_name(
    categories: list[dict[str, Any]],
    name: str,
    *,
    excluding_uuid: str | None = None,
) -> None:
    """要求同一领域包内类别展示名唯一。

    参数：类别全集、候选名称和可选排除身份。返回：无。异常：名称重复时抛
    ``conflict``，防止前端出现两个无法区分的分组。
    """

    folded_name = name.casefold()
    if any(
        item["uuid"] != excluding_uuid and item["name"].casefold() == folded_name
        for item in categories
    ):
        raise OperationCategoryError("conflict")


def _sorted_categories(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按展示顺序、名称和身份返回确定性新列表。

    参数：``categories`` 是已经校验的类别集合。返回：复制并排序后的列表。异常：
    缺失排序字段时原样抛出，调用方只能在完整校验后使用。
    """

    return sorted(
        (dict(item) for item in categories),
        key=lambda item: (item["sort_order"], item["name"], item["uuid"]),
    )


def _sha256(content: bytes) -> str:
    """返回与源码发布器一致的 SHA-256 CAS 令牌。

    参数：``content`` 是原始文件字节。返回：带 ``sha256:`` 前缀的十六进制
    摘要。异常：无。
    """

    return "sha256:" + hashlib.sha256(content).hexdigest()


__all__ = [
    "DEVICE_OPERATION_CATEGORY_UUID",
    "INTEGRATED_OPERATION_CATEGORY_UUID",
    "MANUAL_OPERATION_CATEGORY_UUID",
    "OperationCategoryCatalog",
    "OperationCategoryError",
    "default_operation_categories",
    "legacy_operation_category_uuid",
]
