"""仅内存的 OPC 条件快照判定。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import threading
import time
from typing import Any

from .models import ConditionResult, Trigger, WaitingReason


@dataclass
class _OpcSnapshot:
    """单个 workflow/provider 的变量增量快照。"""

    sequence: int
    values: dict[str, Any] = field(default_factory=dict)
    updated_at_by_variable: dict[str, float] = field(default_factory=dict)


class OpcConditionProvider:
    """保存非持久化 OPC 值，并判定模板的 OPC 触发条件。

    该 provider 只保存变量值、序列号和接收时间，不保存 OPC 地址、账户或密码。
    """

    def __init__(
        self,
        *,
        snapshot_ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if snapshot_ttl_seconds < 0:
            raise ValueError("snapshot_ttl_seconds must be non-negative")
        self._snapshot_ttl_seconds = snapshot_ttl_seconds
        self._clock = clock
        self._snapshots: dict[tuple[str, str], _OpcSnapshot] = {}
        self._lock = threading.RLock()

    def update(
        self,
        workflow_path: str,
        plc_device_id: str,
        sequence: int,
        variables: Mapping[str, Any],
    ) -> bool:
        """合并一批增量变量；重复或过期 sequence 返回 False 且不修改快照。"""
        key = (workflow_path, plc_device_id)
        with self._lock:
            current = self._snapshots.get(key)
            if current is not None and sequence <= current.sequence:
                return False
            values = dict(current.values) if current is not None else {}
            updated_at_by_variable = (
                dict(current.updated_at_by_variable) if current is not None else {}
            )
            values.update(variables)
            received_at = self._clock()
            updated_at_by_variable.update(
                {variable: received_at for variable in variables}
            )
            self._snapshots[key] = _OpcSnapshot(
                sequence=sequence,
                values=values,
                updated_at_by_variable=updated_at_by_variable,
            )
        return True

    def can_update(self, workflow_path: str, plc_device_id: str, sequence: int) -> bool:
        """在不修改快照的前提下判断序列是否会被接受。"""
        with self._lock:
            current = self._snapshots.get((workflow_path, plc_device_id))
            return current is None or sequence > current.sequence

    def export_state(self, workflow_path: str, plc_device_id: str) -> dict[str, Any] | None:
        """导出可持久化的快照状态。"""
        with self._lock:
            snapshot = self._snapshots.get((workflow_path, plc_device_id))
            if snapshot is None:
                return None
            return {
                "plc_device_id": plc_device_id,
                "sequence": snapshot.sequence,
                "values": dict(snapshot.values),
                "updated_at_by_variable": dict(snapshot.updated_at_by_variable),
            }

    def restore_state(self, workflow_path: str, state: Mapping[str, Any]) -> None:
        """从持久化状态恢复单个 provider 快照。"""
        plc_device_id = str(state["plc_device_id"])
        with self._lock:
            self._snapshots[(workflow_path, plc_device_id)] = _OpcSnapshot(
                sequence=int(state["sequence"]),
                values=dict(state["values"]),
                updated_at_by_variable=dict(state["updated_at_by_variable"]),
            )

    def clear_state(self, workflow_path: str, plc_device_id: str) -> None:
        with self._lock:
            self._snapshots.pop((workflow_path, plc_device_id), None)

    def clear_workflow(self, workflow_path: str) -> None:
        """清除工作区全部 PLC 的内存快照及序列水位。"""
        with self._lock:
            self._snapshots = {
                key: snapshot
                for key, snapshot in self._snapshots.items()
                if key[0] != workflow_path
            }

    def evaluate(self, workflow_path: str, trigger: Trigger) -> ConditionResult:
        """判定一个 ``kind='opc'`` Trigger 是否由最新快照满足。"""
        config = trigger.config
        plc_device_id = str(config.get("plc_device_id", ""))
        variable = str(config.get("variable", ""))
        if trigger.kind.lower() != "opc" or not plc_device_id or not variable:
            return ConditionResult(
                satisfied=False,
                reason=WaitingReason(
                    code="opc_condition_invalid",
                    context={"kind": trigger.kind},
                    message="OPC 条件配置无效",
                ),
            )

        with self._lock:
            snapshot = self._snapshots.get((workflow_path, plc_device_id))
            if snapshot is not None:
                snapshot = _OpcSnapshot(
                    sequence=snapshot.sequence,
                    values=dict(snapshot.values),
                    updated_at_by_variable=dict(snapshot.updated_at_by_variable),
                )
        return self._evaluate_snapshot(trigger, snapshot)

    def evaluate_states(
        self,
        trigger: Trigger,
        states: list[Mapping[str, Any]],
    ) -> ConditionResult:
        """直接判定持久化快照，不修改 provider 内存状态。"""
        plc_device_id = str(trigger.config.get("plc_device_id", ""))
        state = next(
            (
                item
                for item in states
                if str(item["plc_device_id"]) == plc_device_id
            ),
            None,
        )
        snapshot = (
            _OpcSnapshot(
                sequence=int(state["sequence"]),
                values=dict(state["values"]),
                updated_at_by_variable=dict(state["updated_at_by_variable"]),
            )
            if state is not None
            else None
        )
        return self._evaluate_snapshot(trigger, snapshot)

    def _evaluate_snapshot(
        self,
        trigger: Trigger,
        snapshot: _OpcSnapshot | None,
    ) -> ConditionResult:
        """对快照副本执行无副作用条件判定。"""
        config = trigger.config
        plc_device_id = str(config.get("plc_device_id", ""))
        variable = str(config.get("variable", ""))
        if trigger.kind.lower() != "opc" or not plc_device_id or not variable:
            return ConditionResult(
                satisfied=False,
                reason=WaitingReason(
                    code="opc_condition_invalid",
                    context={"kind": trigger.kind},
                    message="OPC 条件配置无效",
                ),
            )
        if snapshot is None or variable not in snapshot.values:
            return ConditionResult(
                satisfied=False,
                reason=WaitingReason(
                    code="opc_variable_missing",
                    context={
                        "plc_device_id": plc_device_id,
                        "variable": variable,
                        "expected": config.get("value"),
                        "actual": None,
                        "updated_at": None,
                    },
                    message=f"缺失 OPC 变量：{variable}",
                ),
            )
        if self._is_stale(snapshot.updated_at_by_variable[variable]):
            return ConditionResult(
                satisfied=False,
                reason=WaitingReason(
                    code="opc_snapshot_stale",
                    context={
                        "plc_device_id": plc_device_id,
                        "variable": variable,
                        "expected": config.get("value"),
                        "actual": snapshot.values[variable],
                        "updated_at": snapshot.updated_at_by_variable[variable],
                    },
                    message=f"OPC 快照已陈旧：{plc_device_id}",
                ),
            )

        expected = config.get("value")
        actual = snapshot.values[variable]
        if self._values_equal(actual, expected):
            return ConditionResult(satisfied=True)
        return ConditionResult(
            satisfied=False,
            reason=WaitingReason(
                code="opc_value_mismatch",
                context={
                    "plc_device_id": plc_device_id,
                    "variable": variable,
                    "expected": expected,
                    "actual": actual,
                    "updated_at": snapshot.updated_at_by_variable[variable],
                },
                message=(
                    f"OPC 变量值不匹配：{variable}"
                    f"（期望 {expected}，当前值 {actual}）"
                ),
            ),
        )

    def _is_stale(self, updated_at: float) -> bool:
        return self._clock() - updated_at > self._snapshot_ttl_seconds

    @staticmethod
    def _values_equal(actual: Any, expected: Any) -> bool:
        if isinstance(actual, bool) or isinstance(expected, bool):
            if isinstance(actual, bool) and isinstance(expected, bool):
                return actual == expected
            if isinstance(expected, bool) and isinstance(actual, str):
                return OpcConditionProvider._as_bool(actual) is expected
            return False
        if isinstance(expected, bool):
            return OpcConditionProvider._as_bool(actual) is expected
        if (
            isinstance(expected, (int, float))
            and not isinstance(expected, bool)
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
        ):
            return actual == expected
        if isinstance(expected, str) and isinstance(actual, str):
            return actual == expected
        return actual == expected

    @staticmethod
    def _as_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "on", "yes", "bool", "boolean"}:
                return True
            if normalized in {"false", "0", "off", "no"}:
                return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
        return None
