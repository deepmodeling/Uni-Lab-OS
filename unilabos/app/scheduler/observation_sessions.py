"""观测来源会话：只管理代际、就绪、切换和旧阻断的对账。"""

from __future__ import annotations

import json
from typing import Any, Callable

from unilabos.runtime.observations import Selector, StateError


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ObservationSessions:
    def __init__(self, context, observations):
        self.context, self.observations = context, observations

    def _claimed(self, connection, group):
        return self.context.inventory.has_claims(connection, group["members"])

    def configure_group(self, name: str, selectors: list[Selector]) -> dict[str, Any]:
        if not name or not selectors:
            raise StateError("invalid_request")
        with self.context.transaction() as connection:
            nodes = self.context.native(connection)
            members = {}
            for selector in selectors:
                node = self.observations._select(nodes, selector)
                self.observations._descriptor(node)
                members[node["uuid"]] = node["generation"]
            for row in self.context.db.execute(
                "SELECT name,body FROM observation_source_group"
            ):
                old = json.loads(row["body"])
                if row["name"] == name:
                    if old["members"] == members:
                        return old
                    raise StateError("group_already_configured")
                if set(old["members"]) & set(members):
                    raise StateError("shared_dependency")
            group = dict(
                members=members,
                member_ids={key: nodes[key]["id"] for key in members},
                epoch=0,
                mode="observation",
                provider=None,
                source_session=None,
                runtime_session=self.context.session,
                ready=False,
                ready_members=[],
                recovery_required=False,
                pending=[],
            )
            if self._claimed(connection, group):
                raise StateError("group_busy")
            self.context.save_group(name, group)
            self.context.event("group.configured", {"group": name, **group})
            return group

    def switch(
        self,
        name: str,
        *,
        operation_id: str,
        mode: str,
        provider: str,
        source_session: str,
        prepare: Callable[[], None],
        quiescent: Callable[[], bool],
    ) -> dict[str, Any]:
        """全组 prepare 成功后提交新代际；就绪 ACK 另行处理。

        prepare 只检查能力和资源，不能执行设备动作。请求重试返回同一操作结果。
        """
        if mode not in ("observation", "simulation", "replay") or not all(
            isinstance(value, str) and value
            for value in (operation_id, provider, source_session)
        ):
            raise StateError("invalid_request")
        request = _json([name, mode, provider, source_session])
        with self.context.transaction() as connection:
            old = self.context.db.execute(
                "SELECT request,result FROM observation_source_operation WHERE id=?",
                (operation_id,),
            ).fetchone()
            if old:
                if old["request"] != request:
                    raise StateError("operation_conflict")
                return json.loads(old["result"])
            group = self.context.group(name)
            if (
                group["pending"]
                or group["recovery_required"]
                or self._claimed(connection, group)
                or not quiescent()
            ):
                raise StateError("group_busy")
            nodes = self.context.native(connection)
            for member, generation in group["members"].items():
                if member not in nodes or nodes[member]["generation"] != generation:
                    raise StateError("identity_changed")
                self.observations._descriptor(nodes[member])
            try:
                prepare()
            except Exception as error:
                raise StateError("prepare_failed", str(error)) from error
            group.update(
                epoch=group["epoch"] + 1,
                mode=mode,
                provider=provider,
                source_session=source_session,
                runtime_session=self.context.session,
                ready=False,
                ready_members=[],
            )
            self.context.save_group(name, group)
            result = {
                "operation_id": operation_id,
                "status": "committed",
                "group": name,
                **group,
            }
            self.context.db.execute(
                "INSERT INTO observation_source_operation VALUES (?,?,?)",
                (operation_id, request, _json(result)),
            )
            self.context.event("mode.committed", result)
            return result

    def ready(self, name: str, member: str, epoch: int, source_session: str) -> None:
        with self.context.transaction() as connection:
            group = self.context.group(name)
            nodes = self.context.native(connection)
            if epoch != group["epoch"] or source_session != group["source_session"]:
                raise StateError("stale_source")
            if (
                member not in group["members"]
                or member not in nodes
                or nodes[member]["generation"] != group["members"][member]
            ):
                raise StateError("identity_changed")
            if group["recovery_required"]:
                raise StateError("reconciliation_required")
            group["ready_members"] = sorted(set(group["ready_members"]) | {member})
            group["ready"] = set(group["ready_members"]) == set(group["members"])
            self.context.save_group(name, group)
            self.context.event("worker.ready", {"group": name, **group})

    def disconnect(self, name: str) -> None:
        with self.context.transaction():
            group = self.context.group(name)
            group.update(ready=False, ready_members=[])
            self.context.save_group(name, group)
            self.context.event("worker.degraded", {"group": name})

    def operation(self, operation_id: str) -> dict[str, Any]:
        """查询幂等操作的提交回执以及当前就绪状态。"""
        with self.context.transaction():
            row = self.context.db.execute(
                "SELECT result FROM observation_source_operation WHERE id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise StateError("operation_missing")
            receipt = json.loads(row[0])
            return {"receipt": receipt, "current": self.context.group(receipt["group"])}

    def recover(
        self,
        name: str,
        *,
        quiescent: Callable[[], bool],
        operation_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """持久重启后显式确认已对账；随后仍须新模式操作及完整 ready ACK。"""
        with self.context.transaction() as connection:
            request = _json(["recover", name, evidence])
            if operation_id:
                old = self.context.db.execute(
                    "SELECT request,result FROM observation_source_operation WHERE id=?",
                    (operation_id,),
                ).fetchone()
                if old:
                    if old["request"] != request:
                        raise StateError("operation_conflict")
                    return json.loads(old["result"])
            group = self.context.group(name)
            if group["pending"] or not quiescent() or self._claimed(connection, group):
                raise StateError("reconciliation_required")
            group.update(
                recovery_required=False,
                ready=False,
                ready_members=[],
                epoch=group["epoch"] + 1,
                provider=None,
                source_session=None,
            )
            self.context.save_group(name, group)
            self.context.event("group.recovered", {"group": name, **group})
            if operation_id:
                result = {
                    "kind": "recovery",
                    "operation_id": operation_id,
                    "group": name,
                    "recovery_evidence": evidence,
                    **group,
                }
                self.context.db.execute(
                    "INSERT INTO observation_source_operation VALUES (?,?,?)",
                    (operation_id, request, _json(result)),
                )
                return result
            return group

    def execution_source(self, selector: Selector, *, provider: str) -> dict[str, Any]:
        """返回当前可执行来源；作业准入和去重归 OS 执行权威与命令日志。"""
        with self.context.transaction() as connection:
            nodes = self.context.native(connection)
            node = self.observations._select(nodes, selector)
            found = [
                (row["name"], json.loads(row["body"]))
                for row in self.context.db.execute(
                    "SELECT * FROM observation_source_group"
                )
                if node["uuid"] in json.loads(row["body"])["members"]
            ]
            if not found:
                raise StateError("group_missing")
            name, group = found[0]
            if group["recovery_required"] or not group["ready"]:
                raise StateError("group_not_ready")
            if group["mode"] == "replay" or group["provider"] != provider:
                raise StateError("execution_route_mismatch")
            if any(
                key not in nodes or nodes[key]["generation"] != generation
                for key, generation in group["members"].items()
            ):
                raise StateError("identity_changed")
            if group["pending"]:
                raise StateError("group_busy")
            return dict(
                group=name,
                material_uuid=node["uuid"],
                generation=node["generation"],
                epoch=group["epoch"],
                provider=provider,
                source_session=group["source_session"],
                runtime_session=self.context.session,
            )

    def legacy_action(self, job_id: str) -> dict[str, Any]:
        """读取持久化动作准入与对账状态，不执行或恢复动作。"""
        with self.context.transaction():
            row = self.context.db.execute(
                "SELECT body FROM observation_legacy_hold WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise StateError("action_missing")
            return json.loads(row[0])

    def reconcile_legacy_action(
        self, job_id: str, *, evidence: dict[str, Any], quiescent: Callable[[], bool]
    ) -> dict[str, Any]:
        """包已完成协议/效果对账后解除阻断；Job succeeded 本身不是此证据。"""
        if not evidence or not evidence.get("receipt_id"):
            raise StateError("reconciliation_required")
        if not self.context.inventory.has_receipt(job_id, evidence["receipt_id"]):
            raise StateError("canonical_inventory_receipt_missing")
        with self.context.transaction():
            row = self.context.db.execute(
                "SELECT body FROM observation_legacy_hold WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise StateError("action_missing")
            record = json.loads(row[0])
            if record["state"] == "settled":
                if record["evidence"] != evidence:
                    raise StateError("reconciliation_conflict")
                return record
            if not quiescent():
                raise StateError("group_busy")
            record.update(state="settled", evidence=evidence)
            self.context.db.execute(
                "UPDATE observation_legacy_hold SET body=? WHERE job_id=?",
                (_json(record), job_id),
            )
            group = self.context.group(record["group"])
            group["pending"] = [job for job in group["pending"] if job != job_id]
            self.context.save_group(record["group"], group)
            self.context.event("action.reconciled", record)
            return record
