"""模板投影差量（TemplateProjectionDelta）及其 OS 私有 manifest 持久化。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

ProjectionKind = Literal[
    "node_template",
    "handle_template",
    "resource_template_symbol",
]

TEMPLATE_PROJECTION_COMPILER_VERSION = "registry-template-projection-v1"
_PROJECTION_KINDS = {
    "node_template",
    "handle_template",
    "resource_template_symbol",
}


class TemplateProjectionDeltaError(ValueError):
    """模板投影 manifest 或差量无法安全建立。"""


@dataclass(frozen=True, slots=True)
class TemplateProjectionMember:
    """一个活动模板投影成员的稳定身份与最后变化事实。"""

    authority_id: str
    projection_kind: ProjectionKind
    source_definition_key: str
    business_key: str
    target_uuid: str
    semantic_hash: str
    generation: int

    def __post_init__(self) -> None:
        """验证模板投影成员的持久化不变量。

        参数：无；读取构造字段。
        返回：无。
        异常：权威、种类、来源、业务键、目标 UUID、语义摘要为空，或代际为负数
        时抛出 ``TemplateProjectionDeltaError``。
        """

        string_fields = (
            self.authority_id,
            self.source_definition_key,
            self.business_key,
            self.target_uuid,
            self.semantic_hash,
        )
        if any(
            not isinstance(value, str) or not value.strip() for value in string_fields
        ):
            raise TemplateProjectionDeltaError("模板投影成员身份字段不能为空")
        if self.projection_kind not in _PROJECTION_KINDS:
            raise TemplateProjectionDeltaError("模板投影成员种类非法")
        if not isinstance(self.generation, int) or self.generation < 0:
            raise TemplateProjectionDeltaError("模板投影成员代际不能为负数")

    @property
    def identity(self) -> tuple[str, str]:
        """返回权威内稳定的成员身份。

        参数：无。
        返回：投影种类和稳定业务键组成的二元组。
        异常：无；构造时已完成字段校验。
        """

        return self.projection_kind, self.business_key

    def at_generation(self, generation: int) -> TemplateProjectionMember:
        """返回记录在指定最后变化代际的新成员值。

        参数：``generation`` 是本次提交后投影代际。
        返回：除代际外保持相同的不可变成员副本。
        异常：代际非法时由成员构造校验抛出 ``TemplateProjectionDeltaError``。
        """

        return replace(self, generation=generation)


@dataclass(frozen=True, slots=True)
class TemplateProjectionDelta:
    """一次完整候选相对活动 manifest 的模板投影差量。"""

    authority_id: str
    generation: int
    added: tuple[TemplateProjectionMember, ...]
    modified: tuple[TemplateProjectionMember, ...]
    removed: tuple[TemplateProjectionMember, ...]
    unchanged: tuple[TemplateProjectionMember, ...]

    @property
    def changed(self) -> bool:
        """返回本轮是否需要持久写入并推进代际。

        参数：无。
        返回：存在新增、修改或移除成员时为 ``True``。
        异常：无。
        """

        return bool(self.added or self.modified or self.removed)

    @property
    def active_members(self) -> tuple[TemplateProjectionMember, ...]:
        """返回提交后的完整活动 manifest 成员集合。

        参数：无。
        返回：新增、修改和未变化成员按稳定身份排序的元组；移除成员不再活动。
        异常：无；分类由构造函数保证互斥。
        """

        return tuple(
            sorted(
                (*self.added, *self.modified, *self.unchanged),
                key=_member_sort_key,
            )
        )


def build_template_projection_delta(
    *,
    authority_id: str,
    current_generation: int,
    previous_members: Sequence[TemplateProjectionMember],
    candidate_members: Sequence[TemplateProjectionMember],
) -> TemplateProjectionDelta:
    """比较完整候选和已提交 manifest，产生互斥模板投影差量。

    参数：``authority_id`` 是唯一投影权威（Authority）；``current_generation``
    是提交前代际；``previous_members`` 是当前活动 manifest 全集；
    ``candidate_members`` 是完整编译、已分配目标 UUID 的候选全集。
    返回：稳定排序的新增、修改、移除和未变化分类；只有前三类非空时才把本轮
    代际推进一次，且只给新增和修改成员记录新代际。
    异常：权威或代际非法、成员跨权威、身份重复时抛出
    ``TemplateProjectionDeltaError``，不产生可持久化部分结果。
    """

    if not isinstance(authority_id, str) or not authority_id.strip():
        raise TemplateProjectionDeltaError("模板投影权威不能为空")
    if not isinstance(current_generation, int) or current_generation < 0:
        raise TemplateProjectionDeltaError("模板投影当前代际不能为负数")
    # ``previous_by_identity`` 与 ``candidate_by_identity`` 是两个完整集合的唯一索引。
    previous_by_identity = _member_index(
        authority_id=authority_id,
        members=previous_members,
    )
    candidate_by_identity = _member_index(
        authority_id=authority_id,
        members=candidate_members,
    )
    raw_added: list[TemplateProjectionMember] = []
    raw_modified: list[TemplateProjectionMember] = []
    removed: list[TemplateProjectionMember] = []
    unchanged: list[TemplateProjectionMember] = []
    for identity in sorted(set(previous_by_identity) | set(candidate_by_identity)):
        previous = previous_by_identity.get(identity)
        candidate = candidate_by_identity.get(identity)
        if previous is None and candidate is not None:
            raw_added.append(candidate)
            continue
        if candidate is None and previous is not None:
            removed.append(previous)
            continue
        if previous is None or candidate is None:  # pragma: no cover - 集合并集不变量
            raise TemplateProjectionDeltaError("模板投影差量分类不完整")
        if (
            previous.source_definition_key != candidate.source_definition_key
            or previous.target_uuid != candidate.target_uuid
            or previous.semantic_hash != candidate.semantic_hash
        ):
            raw_modified.append(candidate)
        else:
            # 未变化成员保留其最后变化代际，不能被刷新代际覆盖。
            unchanged.append(previous)
    changed = bool(raw_added or raw_modified or removed)
    next_generation = current_generation + 1 if changed else current_generation
    return TemplateProjectionDelta(
        authority_id=authority_id,
        generation=next_generation,
        added=tuple(item.at_generation(next_generation) for item in raw_added),
        modified=tuple(item.at_generation(next_generation) for item in raw_modified),
        removed=tuple(removed),
        unchanged=tuple(unchanged),
    )


def semantic_template_hash(
    projection_kind: ProjectionKind,
    semantic_value: Any,
) -> str:
    """计算包含编译器版本的规范模板语义摘要。

    参数：``projection_kind`` 区分节点、连接点（Handle）和资源模板
    （ResourceTemplate）源码身份；``semantic_value`` 是完成默认值归一后的持久
    语义值，不得含时间戳或 JSON 顺序噪声。
    返回：带 ``sha256:`` 前缀的稳定摘要；编译器版本升级会使相同来源重新比较。
    异常：种类非法或值无法编码为规范 JSON 时抛出
    ``TemplateProjectionDeltaError``。
    """

    if projection_kind not in _PROJECTION_KINDS:
        raise TemplateProjectionDeltaError("模板投影语义种类非法")
    try:
        encoded = json.dumps(
            {
                "compiler_version": TEMPLATE_PROJECTION_COMPILER_VERSION,
                "projection_kind": projection_kind,
                "semantic_value": semantic_value,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise TemplateProjectionDeltaError("模板投影语义不能规范编码") from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def stable_projection_key(parts: Sequence[str]) -> str:
    """把业务身份各部分编码为无歧义稳定文本。

    参数：``parts`` 是规范业务身份的有序字符串部分。
    返回：紧凑、键序稳定的 JSON 数组文本，可直接持久化为 manifest 业务键。
    异常：输入不是非空字符串序列时抛出 ``TemplateProjectionDeltaError``。
    """

    if isinstance(parts, (str, bytes)) or not isinstance(parts, Sequence):
        raise TemplateProjectionDeltaError("模板投影业务键必须是字符串序列")
    normalized = tuple(parts)
    if not normalized or any(
        not isinstance(part, str) or not part for part in normalized
    ):
        raise TemplateProjectionDeltaError("模板投影业务键部分不能为空")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


class TemplateProjectionManifestPersistence:
    """在模板事务内持久化活动模板投影成员 manifest。"""

    def __init__(self, workflow_store: Any) -> None:
        """绑定工作流存储并建立 OS 私有 manifest 表。

        参数：``workflow_store`` 提供模板表共用的 SQLite 事务接口。
        返回：无；不创建第二个数据库。
        异常：SQLite 建表失败原样传播，调用方必须关闭式停止投影。
        """

        self._workflow_store = workflow_store
        self._initialize_schema()

    def load_in_transaction(
        self,
        connection: Any,
        *,
        authority_id: str,
    ) -> tuple[TemplateProjectionMember, ...]:
        """在当前模板事务内读取一个权威的完整活动 manifest。

        参数：``connection`` 是模板写入共用连接；``authority_id`` 是投影来源。
        返回：按种类和业务键稳定排序的不可变成员元组。
        异常：持久字段不满足成员不变量或身份重复时抛出
        ``TemplateProjectionDeltaError``，不得返回部分 manifest。
        """

        rows = connection.execute(
            """
            SELECT authority_id, projection_kind, source_definition_key,
                   business_key, target_uuid, semantic_hash, generation
            FROM registry_template_projection_member
            WHERE authority_id = ?
            ORDER BY projection_kind, business_key
            """,
            (authority_id,),
        ).fetchall()
        try:
            members = tuple(
                TemplateProjectionMember(
                    authority_id=str(row["authority_id"]),
                    projection_kind=str(row["projection_kind"]),  # type: ignore[arg-type]
                    source_definition_key=str(row["source_definition_key"]),
                    business_key=str(row["business_key"]),
                    target_uuid=str(row["target_uuid"]),
                    semantic_hash=str(row["semantic_hash"]),
                    generation=int(row["generation"]),
                )
                for row in rows
            )
            _member_index(authority_id=authority_id, members=members)
        except (TypeError, ValueError) as error:
            raise TemplateProjectionDeltaError(
                "模板投影成员 manifest 已损坏"
            ) from error
        return members

    def apply_in_transaction(
        self,
        connection: Any,
        *,
        delta: TemplateProjectionDelta,
    ) -> None:
        """在模板写事务内只应用发生变化的 manifest 成员。

        参数：``connection`` 是模板表共用写事务；``delta`` 是完整候选比较结果。
        返回：无；移除成员删除活动 manifest，新增和修改成员写入当前代际，未变化
        成员完全不写，从而保留最后变化代际。
        异常：SQLite 冲突原样传播，由外层事务统一回滚模板和 manifest。
        """

        for member in delta.removed:
            connection.execute(
                """
                DELETE FROM registry_template_projection_member
                WHERE authority_id = ? AND projection_kind = ? AND business_key = ?
                """,
                (member.authority_id, member.projection_kind, member.business_key),
            )
        for member in (*delta.added, *delta.modified):
            connection.execute(
                """
                INSERT INTO registry_template_projection_member(
                    authority_id, projection_kind, source_definition_key,
                    business_key, target_uuid, semantic_hash, generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(authority_id, projection_kind, business_key) DO UPDATE SET
                    source_definition_key = excluded.source_definition_key,
                    target_uuid = excluded.target_uuid,
                    semantic_hash = excluded.semantic_hash,
                    generation = excluded.generation
                """,
                (
                    member.authority_id,
                    member.projection_kind,
                    member.source_definition_key,
                    member.business_key,
                    member.target_uuid,
                    member.semantic_hash,
                    member.generation,
                ),
            )

    def _initialize_schema(self) -> None:
        """建立 OS 私有模板投影成员 manifest 表。

        参数：无。
        返回：无；表与共享工作流模板表位于同一 SQLite 文件。
        异常：SQLite 建表或索引失败原样传播，禁止使用不完整 manifest。
        """

        with self._workflow_store.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS registry_template_projection_member (
                    authority_id TEXT NOT NULL,
                    projection_kind TEXT NOT NULL CHECK(
                        projection_kind IN (
                            'node_template',
                            'handle_template',
                            'resource_template_symbol'
                        )
                    ),
                    source_definition_key TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    target_uuid TEXT NOT NULL,
                    semantic_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    PRIMARY KEY(authority_id, projection_kind, business_key),
                    UNIQUE(authority_id, projection_kind, target_uuid)
                )
                """
            )


def _member_index(
    *,
    authority_id: str,
    members: Sequence[TemplateProjectionMember],
) -> dict[tuple[str, str], TemplateProjectionMember]:
    """建立完整成员集合的唯一身份索引。

    参数：``authority_id`` 是预期权威；``members`` 是完整活动或候选成员集合。
    返回：投影种类与业务键到成员的字典。
    异常：成员类型、权威或身份重复时抛出 ``TemplateProjectionDeltaError``。
    """

    indexed: dict[tuple[str, str], TemplateProjectionMember] = {}
    for member in members:
        if not isinstance(member, TemplateProjectionMember):
            raise TemplateProjectionDeltaError("模板投影成员类型非法")
        if member.authority_id != authority_id:
            raise TemplateProjectionDeltaError("模板投影成员跨越权威")
        if member.identity in indexed:
            raise TemplateProjectionDeltaError("模板投影成员业务身份重复")
        indexed[member.identity] = member
    return indexed


def _member_sort_key(member: TemplateProjectionMember) -> tuple[str, str]:
    """返回模板投影成员的稳定排序键。

    参数：``member`` 是已验证成员。
    返回：投影种类和业务键二元组。
    异常：无。
    """

    return member.identity


__all__ = [
    "TEMPLATE_PROJECTION_COMPILER_VERSION",
    "ProjectionKind",
    "TemplateProjectionDelta",
    "TemplateProjectionDeltaError",
    "TemplateProjectionManifestPersistence",
    "TemplateProjectionMember",
    "build_template_projection_delta",
    "semantic_template_hash",
    "stable_projection_key",
]
