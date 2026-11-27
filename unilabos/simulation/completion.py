"""规范库存提交与设备完成状态之间的通用因果门。"""
from copy import deepcopy
from dataclasses import dataclass

from .motion import CollisionStatus, EvidenceLevel


class EvidenceRejected(ValueError):
    """证据包不满足聚合合同；调用方不得据此升级证据等级或结算。"""


@dataclass(frozen=True)
class EvidenceBundle:
    """OS 汇总的单项动作证据；只引用结算回执与观测标识，不复制物料账本。"""

    action_id: str
    evidence_level: EvidenceLevel
    collision_status: CollisionStatus
    settlement_receipt: str | None = None
    observation_refs: tuple[str, ...] = ()
    failed_reason: str | None = None

    def __post_init__(self):
        """规范化观测引用并校验最小一致性；不通过抛 EvidenceRejected。"""
        if not isinstance(self.observation_refs, tuple):
            object.__setattr__(self, "observation_refs", tuple(self.observation_refs))
        if (
            self.evidence_level.rank >= EvidenceLevel.KINEMATIC_REPLAY.rank
            and not self.settlement_receipt
        ):
            raise EvidenceRejected("settlement_receipt_required")
        if self.evidence_level.rank >= EvidenceLevel.PHYSICAL_QUALIFIED.rank:
            if self.collision_status is not CollisionStatus.CLEAR:
                raise EvidenceRejected("physical_qualification_requires_clear_collision")
            if not self.observation_refs:
                raise EvidenceRejected("physical_qualification_requires_observation")
            if self.failed_reason:
                raise EvidenceRejected("failed_evidence_cannot_be_physical_qualified")


def build_evidence_bundle(
    action_id,
    evidence_level,
    collision_status,
    *,
    settlement_receipt=None,
    observation_refs=(),
    failed_reason=None,
):
    """构造并校验证据包；返回冻结实例，不通过时抛 EvidenceRejected。"""
    return EvidenceBundle(
        action_id=action_id,
        evidence_level=evidence_level,
        collision_status=collision_status,
        settlement_receipt=settlement_receipt,
        observation_refs=observation_refs,
        failed_reason=failed_reason,
    )


class CommitBeforeDone:
    """在设备效果已满足时拦截正 Done，等待当前 Task/Job 的规范库存回执。"""
    def __init__(self, required=False):
        """设置是否启用提交前置门，并清空请求身份、暂存 Done 和回执。"""
        self.required=required
        self.identity=None
        self.receipt=None
        self.intent=None

    def begin(self, identity):
        """为新请求复制身份并清除上一请求的完成意图和回执。"""
        self.identity=deepcopy(identity);self.receipt=None;self.intent=None

    def admit(self, writer, name, value):
        """判断状态写入能否发布；启用门且无回执时暂存正值写入并返回 False。"""
        if self.required and value and self.receipt is None:
            self.intent=(writer,name,value)
            return False
        return True

    def confirm(self, receipt):
        """校验当前请求的规范回执并返回暂存的 writer/name/value。

        必须已有完成意图；回执须具有有效标识、正序号和匹配的 Task/Job。
        相同回执可重确认，冲突回执抛出 ValueError；本方法不执行库存提交。"""
        if (not self.identity or not self.intent or not isinstance(receipt,dict)
                or receipt.get('scope')!='canonical_inventory'
                or not receipt.get('receipt_id') or not receipt.get('effect_id')
                or type(receipt.get('sequence')) is not int or receipt['sequence']<1
                or receipt.get('job_uuid')!=self.identity['node_job_uuid']
                or receipt.get('task_uuid')!=self.identity['task_uuid']):
            raise ValueError('completion_receipt_identity_mismatch')
        if self.receipt is not None and self.receipt!=receipt:
            raise ValueError('completion_receipt_conflict')
        self.receipt=deepcopy(receipt)
        return self.intent
