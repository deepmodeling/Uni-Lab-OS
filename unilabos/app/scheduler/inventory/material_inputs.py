"""中心物料输入的持久占用；跨库建 Task 前先固定唯一任务身份。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from unilabos.app.scheduler.resource_lock import conflicting_resource_lock_keys, device_lock_key, material_lock_key
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.task_package import validate_task_package

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_material_input (
 invocation_uuid TEXT PRIMARY KEY, local_task_uuid TEXT NOT NULL UNIQUE,
 context_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','released'))
);
CREATE TABLE IF NOT EXISTS station_material_input_resource (
 invocation_uuid TEXT NOT NULL REFERENCES station_material_input(invocation_uuid),
 lock_key TEXT NOT NULL, material_uuid TEXT NOT NULL REFERENCES material(uuid),
 PRIMARY KEY(invocation_uuid,lock_key)
);
CREATE INDEX IF NOT EXISTS ix_station_material_input_resource_key
 ON station_material_input_resource(lock_key);
"""


class MaterialInputError(ValueError):
    """输入尚不具备执行条件；保留调用、占用及通知等待核实。"""


class MaterialInputPending(MaterialInputError):
    """本地交接投影尚未同步；保留原通知等待，不生成永久异常。"""


def material_input_blocker(connection: Any, lock_keys: Sequence[str], *, task_uuid: str = "") -> Any:
    """同库存事务检查占用；只有固定 Task 的真实动作能够使用这些资源。"""
    requested = set(lock_keys)
    for row in connection.execute(
        "SELECT h.invocation_uuid,h.local_task_uuid,r.lock_key,r.material_uuid "
        "FROM station_material_input h JOIN station_material_input_resource r USING(invocation_uuid) "
        "WHERE h.state='active' AND h.local_task_uuid<>? ORDER BY h.invocation_uuid,r.lock_key",
        (task_uuid,),
    ).fetchall():
        if conflicting_resource_lock_keys(requested, {row["lock_key"]}):
            return row
    return None


def _unique(values: Sequence[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result = {}
    for value in values:
        identity = value[key]
        if not isinstance(identity, str) or str(UUID(identity)) != identity or identity in result:
            raise MaterialInputError("物料输入身份无效或重复")
        result[identity] = value
    return result


class StationMaterialInputAuthority:
    """复用真实交接收货与库存权威；预留不是 Task 或 Job 执行事实。"""

    def __init__(self, handoffs: Any):
        self.handoffs, self.store = handoffs, handoffs.store

    def prepare(self, package: Mapping[str, Any], *, input_materials: list[dict[str, str]]) -> dict[str, Any]:
        """原子核实全部到货、库存版本和冻结入口，再保留稳定任务身份及资源。"""
        from unilabos.app.scheduler.inventory.dispatch_admission import assert_resource_keys_unclaimed

        body = validate_task_package(package)
        payload = body["payload"]
        if payload["station_uuid"] != self.handoffs.station_uuid or payload["artifact_refs"]:
            raise MaterialInputError("输入不属于本站或包含尚未支持的附件")
        context = {"package": body, "input_materials": input_materials}
        identity = payload["station_invocation_uuid"]
        with self.store.transaction() as c:
            old = c.execute("SELECT * FROM station_material_input WHERE invocation_uuid=?", (identity,)).fetchone()
            if old is not None:
                if digest(json.loads(old["context_json"])) != digest(context) or old["state"] != "active":
                    raise MaterialInputError("原物料输入占用已释放或冻结请求发生变化")
            try:
                resources = self._received_resources(c, payload, input_materials)
            except MaterialInputError:
                raise
            except ValueError as error:
                raise MaterialInputError(str(error)) from error
            except (KeyError, TypeError, AttributeError) as error:
                raise MaterialInputError("物料输入、收货事实或冻结入口不完整") from error
            if old is not None:
                receipt = json.loads(old["receipt_json"])
                self._verify(c, body, receipt)
                if digest(resources) != receipt["resources_digest"]:
                    raise MaterialInputError("原占用资源集合发生变化")
                return receipt
            # 只有经过原谱系核实的本站历史输入可原子接管已补报完成的出站保护。
            if any(r.get("handoff_item_uuid") is None for r in payload["material_refs"]):
                from unilabos.app.scheduler.inventory.outbound import transfer_to_material_input
                transfer_to_material_input(c, references=payload["material_refs"], container_refs=payload["container_refs"])
            assert_resource_keys_unclaimed(c, lock_keys=list(resources))
            from unilabos.app.scheduler.inventory.workflow_quantity import (
                WorkflowQuantityInventoryAuthority,
                active_workflow_reserved_quantities,
            )

            for (kind, inventory_uuid), amount in active_workflow_reserved_quantities(c).items():
                content = c.execute(WorkflowQuantityInventoryAuthority._content_sql(kind), (inventory_uuid,)).fetchone()
                if amount > 0 and (content is None or content["material_uuid"] in set(resources.values())):
                    raise MaterialInputError("收到的容器内容仍有其他任务的数量预留")
            for local in set(resources.values()):
                if c.execute(
                    "SELECT 1 FROM inventory_material_source_binding WHERE material_uuid=? AND status='active' LIMIT 1",
                    (local,),
                ).fetchone() is not None:
                    raise MaterialInputError("收到的物料已被其他本地任务绑定")
            receipt = {"station_invocation_uuid": identity, "task_payload_digest": body["payload_digest"],
                       "local_task_uuid": str(uuid4()), "resources_digest": digest(resources)}
            c.execute("INSERT INTO station_material_input VALUES (?,?,?,?,'active')",
                      (identity, receipt["local_task_uuid"], json.dumps(context, ensure_ascii=False, allow_nan=False),
                       json.dumps(receipt)))
            c.executemany("INSERT INTO station_material_input_resource VALUES (?,?,?)",
                          [(identity, key, local) for key, local in resources.items()])
            return receipt

    def verify(self, package: Mapping[str, Any], receipt: dict[str, Any], *, allow_released: bool = False,
               require_current: bool = False) -> None:
        """恢复必须找到原库存占用；工作流证明不能重建遗失的库存执行权限。"""
        body = validate_task_package(package)
        with self.store.transaction() as c:
            row = self._verify(c, body, receipt, allow_released=allow_released)
            if require_current:
                try:
                    inputs = json.loads(row["context_json"])["input_materials"]
                    resources = self._received_resources(c, body["payload"], inputs)
                except MaterialInputError:
                    raise
                except ValueError as error:
                    raise MaterialInputError(str(error)) from error
                except (KeyError, TypeError, AttributeError) as error:
                    raise MaterialInputError("原占用的冻结输入或当前收货证明不完整") from error
                if digest(resources) != receipt["resources_digest"]:
                    raise MaterialInputError("原占用资源集合发生变化")

    @staticmethod
    def _verify(c: Any, body: dict[str, Any], receipt: dict[str, Any], *, allow_released: bool = False) -> Any:
        from unilabos.center_protocol.material_input import validate_input_receipt

        validate_input_receipt(receipt, body)
        identity = body["payload"]["station_invocation_uuid"]
        row = c.execute("SELECT * FROM station_material_input WHERE invocation_uuid=?", (identity,)).fetchone()
        if (row is None or digest(json.loads(row["receipt_json"])) != digest(receipt)
                or digest(json.loads(row["context_json"])["package"]) != digest(body)
                or (row["state"] != "active" and not allow_released)):
            raise MaterialInputError("原库存输入占用缺失、身份不符或已经释放")
        resources = {r["lock_key"]: r["material_uuid"] for r in c.execute(
            "SELECT lock_key,material_uuid FROM station_material_input_resource WHERE invocation_uuid=?", (identity,),
        ).fetchall()}
        if not resources or digest(resources) != receipt["resources_digest"]:
            raise MaterialInputError("原输入占用的持久资源明细不完整")
        return row

    def release(self, package: Mapping[str, Any], receipt: dict[str, Any]) -> None:
        """由工作流持久终态或领取前拒绝证明驱动；保留历史，不按超时自动放弃。"""
        body = validate_task_package(package)
        with self.store.transaction() as c:
            self._verify(c, body, receipt, allow_released=True)
            c.execute("UPDATE station_material_input SET state='released' WHERE invocation_uuid=?",
                      (body["payload"]["station_invocation_uuid"],))

    def _received_resources(self, c: Any, payload: dict[str, Any], inputs: list[dict[str, str]]) -> dict[str, str]:
        if payload.get("material_source_bindings"):
            from unilabos.app.scheduler.inventory.source_bindings import source_resources
            resources, sources = source_resources(self.handoffs, c, payload)
            merged = {item["local_material_uuid"]: item for item in inputs}
            for source in sources:
                old = merged.setdefault(source["local_material_uuid"], source)
                if old != source:
                    raise MaterialInputError("公开物料入口与静态来源模板不一致")
            if any(payload[name] for name in ("material_refs", "container_refs", "handoff_uuids")):
                # 入口来自两种独立合同验证；历史谱系/完整树校验仍使用同一门禁。
                other = self._received_resources(c, {**payload, "material_source_bindings": []}, list(merged.values()))
                resources.update(other)
            elif inputs:
                raise MaterialInputError("公开物料输入缺少中心冻结引用")
            return resources
        local_refs = [r for r in payload["material_refs"] if r.get("handoff_item_uuid") is None]
        if local_refs:
            local_ids = {r["local_material_uuid"] for r in local_refs}
            local_containers = []
            for container in payload["container_refs"]:
                binding = c.execute("SELECT * FROM container_station_binding WHERE container_uuid=?", (container["container_uuid"],)).fetchone()
                if binding and self.handoffs._subtree(c, binding["carrier_material_uuid"]) & local_ids:
                    local_containers.append(container)
            local_payload = {**payload, "material_refs": local_refs, "container_refs": local_containers, "handoff_uuids": []}
            resources = self._local_resources(c, local_payload, [v for v in inputs if v["local_material_uuid"] in local_ids])
            remote_refs = [r for r in payload["material_refs"] if r.get("handoff_item_uuid") is not None]
            if remote_refs:
                local_container_ids = {v["container_uuid"] for v in local_containers}
                remote = {**payload, "material_refs": remote_refs,
                          "container_refs": [v for v in payload["container_refs"] if v["container_uuid"] not in local_container_ids]}
                other = self._received_resources(c, remote, [v for v in inputs if v["local_material_uuid"] not in local_ids])
                if resources.keys() & other.keys():
                    raise MaterialInputError("本站历史输入与跨站收货不能重复占用同一载体")
                resources.update(other)
            elif payload["handoff_uuids"] or len(local_containers) != len(payload["container_refs"]):
                raise MaterialInputError("本站历史输入包含无关的交接或容器")
            return resources
        refs = _unique(payload["material_refs"], "local_material_uuid")
        _unique(payload["material_refs"], "material_lineage_uuid")
        containers = _unique(payload["container_refs"], "container_uuid")
        ports = _unique(inputs, "local_material_uuid")
        handoffs = payload["handoff_uuids"]
        if (not refs or not containers or not handoffs or len(handoffs) != len(set(handoffs))
                or not ports or not set(ports).issubset(refs)):
            raise MaterialInputError("任务缺少唯一的交接、物料、容器或公开输入")
        expected_refs, expected_containers, resources = {}, {}, {}
        for identity in handoffs:
            row = c.execute("SELECT * FROM station_handoff WHERE handoff_uuid=?", (identity,)).fetchone()
            if row is None:
                raise MaterialInputPending("目标站尚未持久接收全部交接")
            if row["role"] != "target":
                raise MaterialInputError("交接不属于本站目标角色")
            snapshot = self.handoffs._package({"payload": json.loads(row["package_json"]),
                                             "payload_digest": digest(json.loads(row["package_json"]))}, "target")
            if (snapshot.get("global_task_uuid") != payload["global_task_uuid"]
                    or not snapshot.get("transfer_definition_uuid")):
                raise MaterialInputError("交接不属于本次运行")
            if snapshot["attention"] or snapshot["status"] not in {"received", "reconciled"}:
                raise MaterialInputPending("交接尚未同步结算或仍需核实")
            selected_items = [item for item in snapshot["items"] if item["container_uuid"] in containers]
            if not selected_items:
                raise MaterialInputError("任务包含没有选中容器的交接")
            # 每个调用只占用其选中的完整容器；其他容器的后续动作不改变本调用输入。
            for item in selected_items:
                local = c.execute("SELECT * FROM station_handoff_item WHERE handoff_item_uuid=?",
                                  (item["handoff_item_uuid"],)).fetchone()
                if local is None or local["state"] != "settled":
                    raise MaterialInputPending("交接明细尚未在本站结算")
                if local["handoff_uuid"] != identity:
                    raise MaterialInputError("交接明细属于其他交接")
                evidence_refs, evidence = self._target_evidence(c, snapshot, item, local)
                container = item["container_uuid"]
                if container in expected_containers:
                    raise MaterialInputError("同一容器不能重复用于多个交接定义")
                expected_containers[container] = {"container_uuid": container, "state_version": item["current_state_version"]}
                binding = c.execute("SELECT * FROM container_station_binding WHERE container_uuid=?", (container,)).fetchone()
                if (binding is None or binding["station_uuid"] != self.handoffs.station_uuid
                        or binding["carrier_material_uuid"] != local["carrier_material_uuid"]):
                    raise MaterialInputError("实际容器与本站载体映射不匹配")
                subtree = self.handoffs._subtree(c, local["carrier_material_uuid"])
                if subtree != {r["local_material_uuid"] for r in evidence_refs} | {local["carrier_material_uuid"]}:
                    raise MaterialInputError("实际容器子树与原收货引用不一致")
                inventory_snapshot = evidence.get("inventory_snapshot", evidence if evidence.get("adapter") else None)
                if inventory_snapshot is not None:
                    from unilabos.app.scheduler.inventory.handoff_adapter import export_matching_snapshot

                    current = export_matching_snapshot(self.handoffs, c, local["carrier_material_uuid"], evidence_refs, inventory_snapshot)
                    if digest(current) != digest(inventory_snapshot):
                        raise MaterialInputError("当前容器结构、内容或化学目录与收货证明不一致")
                snapshots = _unique(evidence["materials"], "local_material_uuid")
                for reference in evidence_refs:
                    local_uuid = reference["local_material_uuid"]
                    if local_uuid in expected_refs or local_uuid not in subtree:
                        raise MaterialInputError("物料引用重复或不在实际容器中")
                    expected_refs[local_uuid] = {**reference, "handoff_item_uuid": item["handoff_item_uuid"]}
                    material = self.handoffs._material(c, local_uuid)
                    known = snapshots[local_uuid]
                    if (reference["station_uuid"] != self.handoffs.station_uuid
                            or type(reference["local_revision"]) is not int
                            or reference["local_revision"] != material["aggregate_version"]
                            or known["local_revision"] != reference["local_revision"]
                            or known["resource_template_uuid"] != material["resource_template_uuid"]
                            or (local_uuid in ports and ports[local_uuid]["resource_template_uuid"] != material["resource_template_uuid"])):
                        raise MaterialInputError("目标物料版本或冻结入口模板与真实库存不匹配")
                    mapping = c.execute("SELECT * FROM material_station_binding WHERE handoff_item_uuid=? AND material_lineage_uuid=?",
                                        (item["handoff_item_uuid"], reference["material_lineage_uuid"])).fetchone()
                    if mapping is None or mapping["local_material_uuid"] != local_uuid:
                        raise MaterialInputError("原谱系没有对应的本站收货映射")
                for local_uuid in subtree:
                    if self.handoffs._material(c, local_uuid)["inventory_status"] != "warehouse":
                        raise MaterialInputError("整个容器尚未进入可用库存")
                    resources[material_lock_key(local_uuid)] = local_uuid
                    resources[device_lock_key(local_uuid)] = local_uuid
        if digest(expected_refs) != digest(refs) or digest(expected_containers) != digest(containers):
            raise MaterialInputError("任务引用必须精确覆盖选中容器的全部真实收货物料")
        return resources

    def _local_resources(self, c: Any, payload: dict[str, Any], inputs: list[dict[str, str]]) -> dict[str, str]:
        """本站历史输入核对真实库存与原谱系；不创建跨站收货回执。"""
        refs = _unique(payload["material_refs"], "local_material_uuid")
        _unique(payload["material_refs"], "material_lineage_uuid")
        containers = _unique(payload["container_refs"], "container_uuid")
        ports = _unique(inputs, "local_material_uuid")
        if not refs or not containers or not ports or not set(ports).issubset(refs):
            raise MaterialInputError("本站历史输入缺少物料、容器或冻结入口")
        resources, covered = {}, set()
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for identity, expected in containers.items():
            binding = c.execute("SELECT * FROM container_station_binding WHERE container_uuid=?", (identity,)).fetchone()
            if binding is None or binding["station_uuid"] != self.handoffs.station_uuid:
                raise MaterialInputError("本站历史容器没有有效的本地绑定")
            if dict(binding).get("registration_request_json") and not dict(binding).get("registration_response_json"):
                raise MaterialInputError("本站历史容器登记尚未完成")
            subtree = self.handoffs._subtree(c, binding["carrier_material_uuid"])
            known_versions = []
            for row in c.execute("SELECT package_json FROM station_handoff").fetchall():
                snapshot = json.loads(row["package_json"])
                for item in snapshot["items"]:
                    if item["container_uuid"] == identity:
                        known_versions.append(item["current_state_version"])
            if "station_material_export" in tables:
                for row in c.execute("SELECT containers_json FROM station_material_export WHERE completed=1 AND containers_json IS NOT NULL").fetchall():
                    for container in json.loads(row["containers_json"]):
                        if container["container_uuid"] == identity:
                            known_versions.append(container["center_snapshot"]["state_version"])
            if not known_versions or max(known_versions) != expected["state_version"]:
                raise MaterialInputError("本站历史容器版本与持久事实不一致")
            if not (subtree - {binding["carrier_material_uuid"]}).issubset(refs):
                raise MaterialInputError("本站历史输入未覆盖实际容器的完整内容")
            for local in subtree:
                material = self.handoffs._material(c, local)
                if material["inventory_status"] != "warehouse" or material["disposition"] != "active":
                    raise MaterialInputError("本站历史物料不在可用库存中")
                resources[material_lock_key(local)] = local
                resources[device_lock_key(local)] = local
                if local not in refs:
                    continue
                ref = refs[local]
                if (ref["station_uuid"] != self.handoffs.station_uuid
                        or ref["local_revision"] != material["aggregate_version"]
                        or (local in ports and ports[local]["resource_template_uuid"] != material["resource_template_uuid"])):
                    raise MaterialInputError("本站历史物料身份、版本或冻结模板不一致")
                known = c.execute("SELECT 1 FROM material_station_binding WHERE local_material_uuid=? AND material_lineage_uuid=?",
                                  (local, ref["material_lineage_uuid"])).fetchone()
                if known is None and "station_material_export_item" in tables:
                    known = c.execute("SELECT 1 FROM station_material_export_item i JOIN station_material_export e USING(invocation_uuid) WHERE i.local_material_uuid=? AND i.material_lineage_uuid=? AND e.completed=1",
                                      (local, ref["material_lineage_uuid"])).fetchone()
                if known is None:
                    raise MaterialInputError("本站历史物料没有经验证的原谱系映射")
                covered.add(local)
        if covered != set(refs):
            raise MaterialInputError("本站历史引用不属于已验证容器")
        return resources

    @staticmethod
    def _target_evidence(c: Any, snapshot: dict[str, Any], item: dict[str, Any], local: Any) -> tuple[Any, Any]:
        receipt = item["receipt"]
        if receipt is None or local["receipt_json"] is None or digest(receipt) != digest(json.loads(local["receipt_json"])):
            raise MaterialInputError("中心原收货回执与本站持久事实不一致")
        settlement = item.get("settlement")
        if settlement is not None and settlement["decision"] != "target_received":
            raise MaterialInputError("物料尚未结算给目标站")
        review = item.get("movement_review")
        if review is None:
            return receipt["target_material_refs"], receipt["target_snapshot"]
        present, absent = review.get("target_verification"), review.get("source_verification")
        movement = c.execute("SELECT * FROM station_handoff_movement WHERE handoff_item_uuid=? AND review_revision=?",
                             (item["handoff_item_uuid"], review["review_revision"])).fetchone()
        if (settlement is None or present is None or absent is None
                or not present["physical_present"] or absent["physical_present"]
                or present["station_uuid"] != snapshot["target_station_uuid"]
                or present["expected_state_version"] + 1 != item["current_state_version"]
                or movement is None or digest(json.loads(movement["proof_json"])) != digest(present)
                or movement["settlement_json"] is None or digest(json.loads(movement["settlement_json"])) != digest(settlement)):
            raise MaterialInputError("新移动尚无本轮目标在场及源站离场结算证明")
        return present["material_refs"], present["inventory_snapshot"]
