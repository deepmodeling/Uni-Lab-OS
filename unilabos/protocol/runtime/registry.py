"""Registry Authority 协议对象：条目状态、挂起冲突、上报批次统计与按哈希增量上报。

Edge 全量上报后，每个模板条目独立维护版本；被活跃 workflow 节点引用的
action 发生删除/变化时条目挂起（pending），由前端"升级"按钮确认。本模块
是 backend API 与 edge-ui 之间的冻结契约：服务端构造这些模型后输出，
前端 `@openlab/protocol` 的 registry 域按同名字段消费。

上报走内容哈希（``content_sha256`` = ``canonical_hash(条目定义)``，权威为每个版本存好）：

1. Host ``GET /api/v1/registry/digest`` 拿到权威当前持有的 ``{name: content_sha256}``
   （:class:`RegistryDigest`）；首次上报权威一无所有，索引为空。
2. Host ``POST /api/v1/registry/reports`` 发 :class:`RegistryReport`：**每个**条目都带
   名字和哈希（集合本身决定哪些条目要软移除），只有权威没有该哈希的条目才附完整
   ``payload``——所以首次上报会把全部定义发过去，之后每次启动只剩几 KB 的哈希清单。
3. 权威对附了 payload 的条目自己重算哈希（Host 声明的哈希只用于选择，不作数）；
   只发哈希却与权威已有版本对不上的条目列入结果的 ``missing``，Host 补上 payload
   再报一次即可。旧形状（``{"resources": [...]}`` 全量定义列表）继续被接受。
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import Field

from unilabos.protocol.base import JsonObject, NonEmptyStr, ServerObject

REGISTRY_PROTOCOL_VERSION = "runtime.v1"

#: 条目冲突原因：候选版本删除或修改了被引用的 action。
RegistryConflictReason = Literal["action-removed", "action-changed"]

#: 条目组合状态标签（一个条目可同时携带多个，如 active+pending）。
RegistryEntryStatus = Literal["active", "pending", "removed", "unusable"]


class RegistryConflict(ServerObject):
    """挂起冲突明细：哪个 action、因何冲突。"""

    action: str
    reason: RegistryConflictReason


class RegistryEntrySummary(ServerObject):
    """条目状态行（列表/详情共用）。"""

    name: str
    template_uuid: str
    active_version: Optional[int] = None
    pending_version: Optional[int] = None
    pending_conflicts: List[RegistryConflict] = Field(default_factory=list)
    unusable_reason: str = ""
    removed_at_ms: Optional[int] = None
    updated_at_ms: int = 0
    status: List[RegistryEntryStatus] = Field(default_factory=list)


class RegistryAffectedNode(ServerObject):
    """被挂起条目影响的 workflow 画布节点（前端徽标定位依据）。"""

    workflow_uuid: str
    workflow_name: str
    node_uuid: str
    node_name: str
    action: str


class RegistryPendingImpact(ServerObject):
    """一个挂起条目的影响面：冲突明细 + 受影响节点清单。"""

    name: str
    template_uuid: str
    active_version: Optional[int] = None
    pending_version: int
    conflicts: List[RegistryConflict] = Field(default_factory=list)
    affected_nodes: List[RegistryAffectedNode] = Field(default_factory=list)


class RegistryPendingItem(ServerObject):
    """上报批次里新挂起的条目明细。"""

    name: str
    conflicts: List[RegistryConflict] = Field(default_factory=list)


class RegistryUnusableItem(ServerObject):
    """上报批次里的不可用定义（id 缺失/类型非法等，不进版本历史）。"""

    id: str = ""
    reason: str


class RegistryReportCounts(ServerObject):
    """上报批次计数。"""

    total: int = Field(ge=0)
    added: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    revived: int = Field(default=0, ge=0)
    unusable: int = Field(default=0, ge=0)


class RegistryReportSummary(ServerObject):
    """一次 Edge 全量上报的批次统计（计数 + 明细）。"""

    counts: RegistryReportCounts
    added: List[str] = Field(default_factory=list)
    updated: List[str] = Field(default_factory=list)
    pending: List[RegistryPendingItem] = Field(default_factory=list)
    removed: List[str] = Field(default_factory=list)
    revived: List[str] = Field(default_factory=list)
    unusable: List[RegistryUnusableItem] = Field(default_factory=list)


class RegistryDigest(ServerObject):
    """权威当前持有的条目内容哈希索引（``GET /api/v1/registry/digest``）。

    生效版本与挂起版本的哈希都算"已持有"：Host 再报同一份挂起内容不必重传，
    也不会再多生成一个版本。软移除条目的生效哈希也在 ``active`` 里——再报同一哈希即复活。
    """

    protocol_version: Literal["runtime.v1"] = REGISTRY_PROTOCOL_VERSION
    #: name -> 生效版本的 content_sha256
    active: Dict[str, str] = Field(default_factory=dict)
    #: name -> 挂起版本的 content_sha256
    pending: Dict[str, str] = Field(default_factory=dict)

    def holds(self, name: str, content_sha256: str) -> bool:
        return content_sha256 in (self.active.get(name), self.pending.get(name))


class RegistryReportEntry(ServerObject):
    """上报中的一个条目：名字 + 内容哈希，权威没有该哈希时才附完整定义。"""

    id: NonEmptyStr
    content_sha256: NonEmptyStr
    payload: Optional[JsonObject] = None


class RegistryReport(ServerObject):
    """按哈希增量的全量上报（``POST /api/v1/registry/reports`` 新形状）。

    ``entries`` 必须覆盖 Host 当前的全部条目（设备 / 资源 / 工作流模板）：
    不在其中的既有条目会被权威软移除，这一点与旧的全量定义列表一致。
    """

    protocol_version: Literal["runtime.v1"] = REGISTRY_PROTOCOL_VERSION
    edge_uuid: str = ""
    entries: List[RegistryReportEntry] = Field(default_factory=list)


class RegistryTemplateIdentity(ServerObject):
    name: str
    uuid: str


class RegistryReportResult(ServerObject):
    """上报结果：批次号、统计、模板身份，以及权威仍缺定义的条目。"""

    report_id: int
    created_at_ms: int
    summary: RegistryReportSummary
    templates: List[RegistryTemplateIdentity] = Field(default_factory=list)
    #: 只发了哈希、权威却没有对应版本的条目：Host 需附上 payload 再报一次
    missing: List[str] = Field(default_factory=list)


__all__ = [
    "REGISTRY_PROTOCOL_VERSION",
    "RegistryAffectedNode",
    "RegistryConflict",
    "RegistryConflictReason",
    "RegistryDigest",
    "RegistryEntryStatus",
    "RegistryEntrySummary",
    "RegistryPendingImpact",
    "RegistryPendingItem",
    "RegistryReport",
    "RegistryReportCounts",
    "RegistryReportEntry",
    "RegistryReportResult",
    "RegistryReportSummary",
    "RegistryTemplateIdentity",
    "RegistryUnusableItem",
]
