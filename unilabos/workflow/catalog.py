"""已发布源码目录（PublishedSourceCatalog）的不可变纯值实现。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import rfc8785

from unilabos.workflow.models import validate_uuid

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PublishedSourceCatalogError(ValueError):
    """已发布源码身份、唯一性或查询违反封闭目录合同。"""

    def __init__(self, code: str, path: str) -> None:
        """保存稳定错误码和 JSON Pointer 路径。

        参数：``code`` 是公共失败分类，``path`` 定位目录字段。返回：无。
        异常：无；构造函数不回显不可信记录内容。
        """

        self.code = code
        self.path = path
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PublishedWorkflowSource:
    """一个可被组合工作流调用（CompositeWorkflowInvocation）引用的源码身份。"""

    workflow_uuid: str
    definition_fqid: str
    module: str
    symbol: str
    source_uri: str
    package_catalog_digest: str
    definition_content_hash: str


class PublishedSourceCatalog:
    """只按静态模块与符号解析的不可变已发布源码目录。"""

    def __init__(
        self,
        *,
        digest: str,
        sources: Sequence[PublishedWorkflowSource],
    ) -> None:
        """绑定已规范化摘要和来源全集。

        参数：``digest`` 是整代目录摘要；``sources`` 已完成身份校验。返回：无。
        异常：重复解析键由构造函数关闭失败，防止调用方绕过 ``from_records``。
        """

        by_import: dict[tuple[str, str], PublishedWorkflowSource] = {}
        for source in sources:
            key = (source.module, source.symbol)
            if key in by_import:
                raise PublishedSourceCatalogError(
                    "published_source_duplicate",
                    "/sources",
                )
            by_import[key] = source
        self.digest = digest
        self.sources = tuple(sources)
        self._by_import = MappingProxyType(by_import)

    @classmethod
    def from_records(
        cls,
        records: Sequence[Mapping[str, Any]],
    ) -> PublishedSourceCatalog:
        """从启动时已授权的静态 package 来源记录冻结一代目录。

        参数：``records`` 是来源发现/持久存储适配器给出的完整记录，不触发目录
        扫描、Python import 或源码执行。返回：按 ``module/symbol`` 排序且带 RFC
        8785 SHA-256 摘要的目录。异常：字段、身份、哈希或唯一性不合法时抛出
        ``PublishedSourceCatalogError``，不返回部分目录。
        """

        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise PublishedSourceCatalogError("published_source_invalid", "/sources")
        normalized = [
            _normalize_record(record, index=index)
            for index, record in enumerate(records)
        ]
        normalized.sort(key=lambda item: (item["module"], item["symbol"]))
        seen_imports: set[tuple[str, str]] = set()
        seen_workflows: set[str] = set()
        for index, item in enumerate(normalized):
            import_key = (item["module"], item["symbol"])
            if import_key in seen_imports or item["workflow_uuid"] in seen_workflows:
                raise PublishedSourceCatalogError(
                    "published_source_duplicate",
                    f"/sources/{index}",
                )
            seen_imports.add(import_key)
            seen_workflows.add(item["workflow_uuid"])
        digest = "sha256:" + hashlib.sha256(rfc8785.dumps(normalized)).hexdigest()
        sources = tuple(
            PublishedWorkflowSource(
                **item,
                package_catalog_digest=digest,
            )
            for item in normalized
        )
        return cls(digest=digest, sources=sources)

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource:
        """按绝对模块与静态符号取得唯一已发布工作流来源。

        参数：``module`` 和 ``symbol`` 来自只读 AST import/call。返回：冻结来源。
        异常：查询身份非法或不存在时抛出稳定目录错误，不进行模糊匹配。
        """

        if not _absolute_module(module) or not _identifier(symbol):
            raise PublishedSourceCatalogError(
                "published_source_invalid",
                "/resolve",
            )
        try:
            return self._by_import[(module, symbol)]
        except KeyError:
            raise PublishedSourceCatalogError(
                "published_source_not_found",
                "/resolve",
            ) from None


def _normalize_record(record: Mapping[str, Any], *, index: int) -> dict[str, str]:
    """校验并复制一项已发布源码记录。

    参数：``record`` 是可疑来源对象，``index`` 用于错误路径。返回：闭合规范字典。
    异常：任一字段缺失、额外或非法时抛出 ``PublishedSourceCatalogError``。
    """

    fields = {
        "workflow_uuid",
        "definition_fqid",
        "module",
        "symbol",
        "source_uri",
        "definition_content_hash",
    }
    path = f"/sources/{index}"
    if not isinstance(record, Mapping) or set(record) != fields:
        raise PublishedSourceCatalogError("published_source_invalid", path)
    try:
        workflow_uuid = validate_uuid(record["workflow_uuid"])
    except (TypeError, ValueError):
        raise PublishedSourceCatalogError(
            "published_source_invalid",
            f"{path}/workflow_uuid",
        ) from None
    module = record["module"]
    symbol = record["symbol"]
    definition_fqid = record["definition_fqid"]
    source_uri = record["source_uri"]
    content_hash = record["definition_content_hash"]
    if (
        not _absolute_module(module)
        or not _identifier(symbol)
        or not _absolute_module(definition_fqid)
        or not isinstance(source_uri, str)
        or not source_uri.startswith("package://")
        or not isinstance(content_hash, str)
        or _SHA256.fullmatch(content_hash) is None
    ):
        raise PublishedSourceCatalogError("published_source_invalid", path)
    return {
        "workflow_uuid": workflow_uuid,
        "definition_fqid": definition_fqid,
        "module": module,
        "symbol": symbol,
        "source_uri": source_uri,
        "definition_content_hash": content_hash,
    }


def _absolute_module(value: Any) -> bool:
    """判断字符串是否由一个或多个 Python 标识符组成且不是相对模块。

    参数：``value`` 是待校验值。返回：绝对模块身份合法时为 ``True``。
    异常：无；非字符串和值为空时返回 ``False``。
    """

    return isinstance(value, str) and bool(value) and all(
        _identifier(part) for part in value.split(".")
    )


def _identifier(value: Any) -> bool:
    """判断值是否为非关键字约束之外的静态 Python 标识符。

    参数：``value`` 是待校验值。返回：非空 Python 标识符时为 ``True``。
    异常：无；非字符串和值为空时返回 ``False``。
    """

    return isinstance(value, str) and bool(value) and value.isidentifier()


__all__ = [
    "PublishedSourceCatalog",
    "PublishedSourceCatalogError",
    "PublishedWorkflowSource",
]
