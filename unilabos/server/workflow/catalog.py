"""已发布工作流源码目录的不可变纯值实现。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from unilabos.server.workflow.json_codec import encode_json
from unilabos.server.workflow.models import validate_uuid

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PublishedSourceCatalogError(ValueError):
    """已发布源码身份、唯一性或查询违反封闭目录合同。"""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PublishedWorkflowSource:
    """一个可被组合工作流调用引用的冻结源码身份。"""

    workflow_uuid: str
    definition_fqid: str
    module: str
    symbol: str
    source_uri: str
    package_catalog_digest: str
    definition_content_hash: str


class PublishedSourceCatalog:
    """只按静态模块和符号解析的不可变已发布源码目录。"""

    def __init__(
        self,
        *,
        digest: str,
        sources: Sequence[PublishedWorkflowSource],
    ) -> None:
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
    ) -> "PublishedSourceCatalog":
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise PublishedSourceCatalogError("published_source_invalid", "/sources")
        normalized = [
            _normalize_record(record, index=index)
            for index, record in enumerate(records)
        ]
        normalized.sort(key=lambda item: (item["module"], item["symbol"]))
        imports: set[tuple[str, str]] = set()
        workflows: set[str] = set()
        for index, item in enumerate(normalized):
            import_key = (item["module"], item["symbol"])
            if import_key in imports or item["workflow_uuid"] in workflows:
                raise PublishedSourceCatalogError(
                    "published_source_duplicate",
                    f"/sources/{index}",
                )
            imports.add(import_key)
            workflows.add(item["workflow_uuid"])
        digest = "sha256:" + hashlib.sha256(
            encode_json(normalized, sort_keys=True)
        ).hexdigest()
        return cls(
            digest=digest,
            sources=tuple(
                PublishedWorkflowSource(
                    **item,
                    package_catalog_digest=digest,
                )
                for item in normalized
            ),
        )

    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource:
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
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and all(_identifier(part) for part in value.split("."))
    )


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and value.isidentifier()


__all__ = [
    "PublishedSourceCatalog",
    "PublishedSourceCatalogError",
    "PublishedWorkflowSource",
]
