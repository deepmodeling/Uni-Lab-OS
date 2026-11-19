"""Registry Authority 协议对象：条目状态、挂起冲突、上报批次统计与按哈希增量上报。

Edge 全量上报后，每个模板条目独立维护版本；不满足自动接受规则的变化挂起
（pending），由前端确认或忽略；变化删改了活跃 workflow 引用的 action 时附冲突明细
（受影响节点见 pending-impacts）。本模块
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

接受规则（权威不自己扫描注册表，目录只来自上报）：上报里新增或变化的条目先挂起，除非
命中自动接受规则——规则 A：条目所属驱动包的版本号比已接受版本的包版本高；规则 B：单机
部署（上报的 Host 与权威同一台机器、且权威没有记录到跨机器 Slave）时全部接受。为此每个
条目带 ``package`` / ``package_version``（不进内容哈希），上报带 Host 的 ``machine_id``
与当前连着的 Slave（:class:`RegistrySlave`）。
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

#: 挂起的是哪种变化：新条目、已有条目的新版本、Host 不再上报（待确认移除）。
RegistryPendingChange = Literal["", "add", "update", "remove"]

#: 为什么没有自动接受：上报的 Host 不在权威这台机器上 / 权威记录着跨机器 Slave。
RegistryPendingReason = Literal["", "cross-machine-reporter", "cross-machine-slave"]

#: 自动接受依据：规则 B（单机部署）/ 规则 A（驱动包版本升高）。
RegistryAcceptRule = Literal["single-machine", "package-upgrade"]


class RegistryConflict(ServerObject):
    """挂起冲突明细：哪个 action、因何冲突。"""

    action: str
    reason: RegistryConflictReason


class RegistryEntrySummary(ServerObject):
    """条目状态行（列表/详情共用）。

    ``package`` / ``package_version`` 是生效内容最近一次被上报时所属的驱动包（规则 A 的比较基准）；
    ``pending_package_version`` 是挂起版本上报时的包版本。
    """

    name: str
    template_uuid: str
    active_version: Optional[int] = None
    pending_version: Optional[int] = None
    pending_conflicts: List[RegistryConflict] = Field(default_factory=list)
    pending_change: RegistryPendingChange = ""
    pending_reason: RegistryPendingReason = ""
    pending_package_version: str = ""
    package: str = ""
    package_version: str = ""
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
    """一个挂起条目的影响面：冲突明细 + 受影响节点清单。

    待确认移除（``pending_change="remove"``）没有挂起版本，``pending_version`` 为 0。
    """

    name: str
    template_uuid: str
    active_version: Optional[int] = None
    pending_version: int
    pending_change: RegistryPendingChange = ""
    pending_reason: RegistryPendingReason = ""
    conflicts: List[RegistryConflict] = Field(default_factory=list)
    affected_nodes: List[RegistryAffectedNode] = Field(default_factory=list)


class RegistryPendingItem(ServerObject):
    """上报批次里新挂起的条目明细。"""

    name: str
    conflicts: List[RegistryConflict] = Field(default_factory=list)
    change: RegistryPendingChange = ""
    reason: RegistryPendingReason = ""


class RegistryAcceptedItem(ServerObject):
    """上报批次里按自动接受规则直接生效的变化（新增 / 更新 / 移除）。

    ``conflicts`` 非空表示这次变化改动或删掉了活跃 workflow 引用的动作：规则命中时照样生效，
    这里留下记录供前端提示。
    """

    name: str
    rule: RegistryAcceptRule
    conflicts: List[RegistryConflict] = Field(default_factory=list)


class RegistryAcceptance(ServerObject):
    """一次上报的规则 B 判定：是否单机部署，以及不是时的原因。"""

    single_machine: bool
    reason: RegistryPendingReason = ""
    #: 上报 Host 的机器标识（进程内上报为权威自己的）
    reporter_machine_id: str = ""
    #: 判定时权威记录着的跨机器 Slave（machine_name）
    remote_slaves: List[str] = Field(default_factory=list)


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
    #: 已生效的新增 / 更新 / 移除里按自动接受规则生效的个数（与 added / updated / removed 重叠）
    auto_accepted: int = Field(default=0, ge=0)


class RegistryReportSummary(ServerObject):
    """一次 Edge 全量上报的批次统计（计数 + 明细）。"""

    counts: RegistryReportCounts
    added: List[str] = Field(default_factory=list)
    updated: List[str] = Field(default_factory=list)
    pending: List[RegistryPendingItem] = Field(default_factory=list)
    removed: List[str] = Field(default_factory=list)
    revived: List[str] = Field(default_factory=list)
    unusable: List[RegistryUnusableItem] = Field(default_factory=list)
    auto_accepted: List[RegistryAcceptedItem] = Field(default_factory=list)
    acceptance: Optional[RegistryAcceptance] = None


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
    """上报中的一个条目：名字 + 内容哈希，权威没有该哈希时才附完整定义。

    ``package`` / ``package_version``：条目来自哪个驱动包（Host 的驱动包台账、源码树
    ``pyproject.toml`` 或 unilabos 本身）。只用于规则 A，不进内容哈希；只发哈希的条目也带。
    """

    id: NonEmptyStr
    content_sha256: NonEmptyStr
    payload: Optional[JsonObject] = None
    package: str = ""
    package_version: str = ""


class RegistrySlave(ServerObject):
    """Host 当前连着的一个 Slave：HostLink 节点名 + 它自报的机器标识（旧 Slave 没有）。"""

    machine_name: NonEmptyStr
    machine_id: str = ""


class RegistryReport(ServerObject):
    """按哈希增量的全量上报（``POST /api/v1/registry/reports`` 新形状）。

    ``entries`` 必须覆盖 Host 当前的全部条目（设备 / 资源 / 工作流模板）：
    不在其中的既有条目会被权威软移除，这一点与旧的全量定义列表一致。
    """

    protocol_version: Literal["runtime.v1"] = REGISTRY_PROTOCOL_VERSION
    edge_uuid: str = ""
    #: 上报 Host 的机器标识（``utils/machine_identity.py``）；缺省按跨机器处理
    machine_id: str = ""
    #: 上报时 Host 连着的 Slave；跨机器的会被权威记下（之后也可经 ``POST /registry/slaves`` 补报）
    slaves: List[RegistrySlave] = Field(default_factory=list)
    entries: List[RegistryReportEntry] = Field(default_factory=list)


class RegistrySlaveAnnouncement(ServerObject):
    """Host 告知权威它连着的 Slave（``POST /api/v1/registry/slaves``）。"""

    edge_uuid: str = ""
    machine_id: str = ""
    slaves: List[RegistrySlave] = Field(default_factory=list)


class RegistryPendingSelection(ServerObject):
    """批量确认 / 忽略挂起条目：``names`` 为空时作用于全部挂起条目。"""

    names: List[str] = Field(default_factory=list)


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
    "RegistryAcceptRule",
    "RegistryAcceptance",
    "RegistryAcceptedItem",
    "RegistryAffectedNode",
    "RegistryConflict",
    "RegistryConflictReason",
    "RegistryDigest",
    "RegistryEntryStatus",
    "RegistryEntrySummary",
    "RegistryPendingChange",
    "RegistryPendingImpact",
    "RegistryPendingItem",
    "RegistryPendingReason",
    "RegistryPendingSelection",
    "RegistryReport",
    "RegistryReportCounts",
    "RegistryReportEntry",
    "RegistryReportResult",
    "RegistryReportSummary",
    "RegistrySlave",
    "RegistrySlaveAnnouncement",
    "RegistryTemplateIdentity",
    "RegistryUnusableItem",
]
