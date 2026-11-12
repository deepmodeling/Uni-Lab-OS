"""待人工决策的动作异常（``/api/v1/error-decisions``）：清单汇总与放行投递的单一入口。

三类来源报告同形：本进程执行面挂起的失败 attempt、执行面在 Host 子进程时控制面登记的失败、
调度器重启后接管的执行态未知 / 决策上下文丢失的 attempt。放行按归属分派到对应的放行通道。

V1 合同（``app/scheduler/api.py``）：清单 ``{"decisions": [...]}``；放行载荷按 V1 ``ErrorDecisionIn``
组装（``reason`` 恒带，``option`` / ``action`` / ``result`` 给出才带，都不给时由执行面按 V1 设备侧
同口径缺省为 ``abort``）；成功 ``{"decision_id", "status": "delivered"}``；决策已不在等待（被处理、
超时或设备离线）是"不存在"（V1 HTTP 404，经信封按路由口径换算）。

dev 的增量扩展（V1 客户端不发送时行为不变）：可选 ``Idempotency-Key`` 同键同内容重放首次结果、同键
不同内容冲突；可选操作员身份随决策载荷记录；物理结果未知的失败只允许补记结果或终止（3003）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Mapping, Optional



class ErrorDecisionConflict(RuntimeError):
    """幂等键冲突或物理结算限制（业务码 3003）。"""


class ErrorDecisionNotPending(LookupError):
    """决策已不在等待（被处理、超时或设备离线）：V1 的 HTTP 404，由路由按其错误口径换算。"""


class ErrorDecisionUnavailable(RuntimeError):
    """执行面 / 调度器尚未就绪（业务码 5001）。"""

#: 物理结果未知的失败只允许补记实际结果或终止，禁止直接重试
PHYSICAL_SETTLEMENT_ACTIONS = frozenset({"operator_intervention", "abort"})
_IDEMPOTENCY_LIMIT = 2048


class _ReplayLedger:
    """进程内的放行幂等账本：决策本身在执行面内存里，账本与之同寿命。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[str, str, Dict[str, Any]]]" = OrderedDict()

    def get(self, key: str) -> Optional[tuple[str, str, Dict[str, Any]]]:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, decision_id: str, fingerprint: str, result: Dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = (decision_id, fingerprint, result)
            while len(self._entries) > _IDEMPOTENCY_LIMIT:
                self._entries.popitem(last=False)


_LEDGER = _ReplayLedger()


def _fingerprint(decision_id: str, decision: Mapping[str, Any]) -> str:
    material = {key: value for key, value in decision.items() if key != "operator_id"}
    encoded = json.dumps({"decision_id": decision_id, "decision": material}, sort_keys=True,
                         ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ErrorDecisionDesk:
    """按调度器 / 执行面 / 控制面三方汇总与放行；构造时只拿 getter，按请求解析。"""

    def __init__(
        self,
        get_scheduler: Callable[[], Any],
        get_execution_backend: Callable[[], Any],
        get_edge_control: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._get_scheduler = get_scheduler
        self._get_execution_backend = get_execution_backend
        if get_edge_control is None:
            from unilabos.server.backend.edge_control import get_edge_control_service

            get_edge_control = get_edge_control_service
        self._get_edge_control = get_edge_control

    # 清单 -----------------------------------------------------------------

    def items(self) -> List[Dict[str, Any]]:
        backend = self._get_execution_backend()
        scheduler = self._get_scheduler()
        edge_control = self._get_edge_control()
        if backend is None and scheduler is None and edge_control is None:
            raise ErrorDecisionUnavailable("backend execution service is not ready")
        items: List[Dict[str, Any]] = []
        if backend is not None:
            items.extend(backend.list_error_decisions())
        elif edge_control is not None:
            # 执行面在 Host 进程：它打开终态闸门、等本进程放行的失败登记在控制面服务里
            items.extend(edge_control.list_error_decisions())
        lister = getattr(scheduler, "list_reconciliation_decisions", None)
        if callable(lister):
            items.extend(lister())
        # 执行端仍在等待决策时也展示冻结物理范围，不能把未知物理结果提供为普通重试。
        recovery = getattr(scheduler, "_physical_recovery", None)
        if recovery is not None:
            for item in items:
                job_uuid = item.get("job_id")
                record = recovery.get(job_uuid) if job_uuid else None
                if record and (record["request"].get("moves") or record["request"].get("aliquot")):
                    item["category"] = "physical_settlement"
                    item["material_execution"] = record
                    item["options"] = [
                        option for option in item.get("options", [])
                        if option.get("action") in PHYSICAL_SETTLEMENT_ACTIONS
                    ]
        return items

    def find(self, decision_id: str) -> Optional[Dict[str, Any]]:
        return next((item for item in self.items() if item.get("decision_id") == decision_id), None)

    # 放行 -----------------------------------------------------------------

    def deliver(self, decision_id: str, decision: Dict[str, Any]) -> bool:
        """把决策投递给持有该决策的通道；不在等待时返回 False。"""

        # dev 执行面只接受"调度权威已更新"的放行（与改造前的路由缺省一致）
        decision = {**decision}
        decision.setdefault("scheduler_updated", True)
        scheduler = self._get_scheduler()
        edge_control = self._get_edge_control()
        owns = getattr(scheduler, "has_reconciliation_decision", None)
        if callable(owns) and owns(decision_id):
            return bool(scheduler.resolve_reconciliation_decision(decision_id, decision))
        backend = self._get_execution_backend()
        if backend is None and edge_control is not None:
            # 执行面在 Host 进程：本进程是调度权威，经 runtime.v1 命令放行终态闸门
            return bool(edge_control.resolve_error_decision(decision_id, decision))
        if backend is None:
            raise ErrorDecisionUnavailable("backend execution service is not ready")
        return bool(backend.resolve_error_decision(decision_id, decision))

    def resolve(
        self,
        decision_id: str,
        decision: Dict[str, Any],
        *,
        idempotency_key: str = "",
        operator_id: str = "",
    ) -> Dict[str, Any]:
        decision = dict(decision)
        key = str(idempotency_key or "").strip()
        fingerprint = _fingerprint(decision_id, decision)
        if key:
            replay = _LEDGER.get(key)
            if replay is not None:
                if replay[0] != decision_id or replay[1] != fingerprint:
                    raise ErrorDecisionConflict("幂等键已用于另一项决策")
                return dict(replay[2])
        if operator_id:
            decision["operator_id"] = operator_id
        report = self.find(decision_id)
        if report and report.get("category") == "physical_settlement":
            action = decision.get("action") or (decision.get("option") or {}).get("action")
            if action not in PHYSICAL_SETTLEMENT_ACTIONS or not str(decision.get("reason") or "").strip():
                raise ErrorDecisionConflict("物理结果未知，须填写核实说明并补记实际结果或终止任务，禁止直接重试")
        if not self.deliver(decision_id, decision):
            raise ErrorDecisionNotPending(
                f"decision {decision_id} not pending (resolved / timed out / device gone)"
            )
        # 响应保持 V1 形状；操作员身份随决策载荷交给执行面记录
        result = {"decision_id": decision_id, "status": "delivered"}
        if key:
            _LEDGER.put(key, decision_id, fingerprint, result)
        return result


def default_desk() -> ErrorDecisionDesk:
    """本进程装配的调度器 / 执行面 getter（工作流路由投递干预决定时使用）。"""

    from unilabos.server.backend.composition import get_execution_backend, get_scheduler

    return ErrorDecisionDesk(get_scheduler, get_execution_backend)


__all__ = [
    "ErrorDecisionConflict",
    "ErrorDecisionDesk",
    "ErrorDecisionNotPending",
    "ErrorDecisionUnavailable",
    "PHYSICAL_SETTLEMENT_ACTIONS",
    "default_desk",
]
