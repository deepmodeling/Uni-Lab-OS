"""库存权威的结果引用导出；先保存版本快照和重试意图，再请求中心谱系。"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.material import MaterialAllocationError, canonical_uuid, validate_allocation_receipt


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class InventoryMaterialAuthority:
    """库存与工作流分库：库存只提交自己的事实，补报成功标记可在崩溃后重试。"""

    def __init__(self, store: InventoryStore, *, station_uuid: str, instance_uuid: str, event_epoch_uuid: str,
                 now_ms: Callable[[], int] | None = None):
        self.store = store
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.station_uuid = canonical_uuid(station_uuid)
        self.instance_uuid = canonical_uuid(instance_uuid)
        self.event_epoch_uuid = canonical_uuid(event_epoch_uuid)
        with store.transaction() as c:
            # 仅显式安装适配器时启用附加表；两库使用相同 SQL，建表与身份验证同事务。
            c.execute("""CREATE TABLE IF NOT EXISTS station_material_export_identity (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity_json TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS station_material_export (
                invocation_uuid TEXT PRIMARY KEY, local_task_uuid TEXT NOT NULL UNIQUE,
                source_digest TEXT NOT NULL, source_json TEXT NOT NULL,
                request_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                response_json TEXT, evidence_json TEXT, containers_json TEXT, last_error TEXT,
                retry_json TEXT NOT NULL DEFAULT '{}',
                completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)))""")
            c.execute("""CREATE TABLE IF NOT EXISTS station_material_export_item (
                invocation_uuid TEXT NOT NULL REFERENCES station_material_export(invocation_uuid),
                local_material_uuid TEXT NOT NULL REFERENCES material(uuid),
                material_lineage_uuid TEXT,
                PRIMARY KEY(invocation_uuid,local_material_uuid))""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_material_export_local ON station_material_export_item(local_material_uuid)")
            c.execute("""CREATE TABLE IF NOT EXISTS station_material_boundary_receipt (
                invocation_uuid TEXT PRIMARY KEY REFERENCES station_material_export(invocation_uuid),
                receipt_json TEXT NOT NULL)""")
            columns = {row["name"] for row in c.execute("PRAGMA table_info(station_material_export)")}
            if "retry_json" not in columns:
                c.execute("ALTER TABLE station_material_export ADD COLUMN retry_json TEXT NOT NULL DEFAULT '{}'")
            identity = _json([station_uuid, instance_uuid, event_epoch_uuid])
            row = c.execute("SELECT identity_json FROM station_material_export_identity WHERE singleton=1").fetchone()
            if row is not None and row[0] != identity:
                raise MaterialAllocationError("库存物料导出的实例或纪元改变，必须显式核实恢复")
            c.execute("INSERT OR IGNORE INTO station_material_export_identity VALUES (1,?)", (identity,))

    def produced_registration_facts(self, outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """只导出已登记产物的条码及生产证据，存储结构不外泄给中心桥。"""
        result = []
        with self.store.read_connection() as c:
            for output in outputs:
                row = c.execute("SELECT uuid,barcode,meta_data FROM material WHERE uuid=? AND deleted_at IS NULL",
                                (output["local_material_uuid"],)).fetchone()
                production = json.loads(row["meta_data"]).get("runtime_production") if row else None
                if isinstance(production, dict):
                    result.append({"local_material_uuid": row["uuid"], "barcode": row["barcode"],
                                   "evidence_refs": list(production["evidence_refs"])})
        return result

    def begin_export(self, source: dict[str, Any]) -> None:
        """先保存真实调用的导出尝试；JSON null 表示尚未准备，不能作为可发送申请。"""
        if (set(source) != {"invocation_uuid", "local_task_uuid", "station_uuid", "owner_instance_uuid",
                            "event_epoch_uuid", "task_payload_digest", "input_material_refs", "outputs"}
                or (source["station_uuid"], source["owner_instance_uuid"], source["event_epoch_uuid"]) !=
                   (self.station_uuid, self.instance_uuid, self.event_epoch_uuid)):
            raise MaterialAllocationError("结果导出与本站持久 owner 或纪元不一致")
        invocation = canonical_uuid(source["invocation_uuid"])
        canonical_uuid(source["local_task_uuid"])
        frozen = {"source": deepcopy(source), "origins": None}
        with self.store.transaction() as c:
            old = c.execute("SELECT * FROM station_material_export WHERE invocation_uuid=?", (invocation,)).fetchone()
            if old is not None:
                if digest(json.loads(old["source_json"])["source"]) != digest(source):
                    raise MaterialAllocationError("调用的真实输出已经冻结，禁止改写")
                return
            c.execute("""INSERT INTO station_material_export(invocation_uuid,local_task_uuid,source_digest,
                source_json,request_json,snapshot_json) VALUES (?,?,?,?,?,?)""",
                (invocation, source["local_task_uuid"], digest(frozen), _json(frozen), "null", "null"))

    def prepare(self, source: dict[str, Any], *, origins: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """尝试先落盘，校验后原子冻结库存修订和完整申请；失败不留下可发送的半成品。"""
        self.begin_export(source)
        invocation = source["invocation_uuid"]
        origins = deepcopy(origins or {})
        if set(origins) - {item["local_material_uuid"] for item in source["outputs"]}:
            raise MaterialAllocationError("谱系提案包含未声明的输出物料")
        source = deepcopy(source)
        frozen = {"source": source, "origins": origins}
        with self.store.transaction() as c:
            old = c.execute("SELECT * FROM station_material_export WHERE invocation_uuid=?", (invocation,)).fetchone()
            if old["request_json"] != "null":
                if old["source_digest"] != digest(frozen):
                    raise MaterialAllocationError("调用的真实输出、谱系来源或稳定键已经冻结，禁止改写")
                self._clear_failure_in(c, invocation, "prepare")
                return self._intent(old)
            if len(source["outputs"]) > 100:
                raise MaterialAllocationError("结果物料数量超过 100")
            proposals, snapshots, identities = [], [], set()
            input_refs = {r["local_material_uuid"]: r for r in source["input_material_refs"]}
            for output in source["outputs"]:
                local = canonical_uuid(output["local_material_uuid"])
                if local in identities:
                    raise MaterialAllocationError("同一物料不能占用多个输出身份")
                identities.add(local)
                row = c.execute("SELECT * FROM material WHERE uuid=? AND deleted_at IS NULL", (local,)).fetchone()
                if row is None:
                    raise MaterialAllocationError("真实输出物料在本站库存不存在或已删除")
                material = dict(row)
                revision = material["aggregate_version"]
                if type(revision) is not int or not 1 <= revision <= 2**53 - 1:
                    raise MaterialAllocationError("库存权威的物料修订无效")
                if material["resource_template_uuid"] != output["resource_template_uuid"]:
                    raise MaterialAllocationError("实际库存模板与冻结输出端口不匹配")
                if material["inventory_status"] in {"incoming", "incoming_canceled", "handoff_departed"}:
                    raise MaterialAllocationError("未接收或已离站的物料不能成为可交付输出")
                known = c.execute("SELECT 1 FROM material_station_binding WHERE local_material_uuid=? LIMIT 1", (local,)).fetchone()
                # 已提交申请也占用原身份，网络暂未返回时不能由另一个调用再申请。
                known = known or c.execute("SELECT 1 FROM station_material_export_item WHERE local_material_uuid=? LIMIT 1", (local,)).fetchone()
                input_ref = input_refs.get(local)
                origin = origins.get(local)
                if origin is None:
                    if input_ref is not None:
                        origin = {"kind": "retained", "material_lineage_uuid": input_ref["material_lineage_uuid"]}
                    elif known:
                        raise MaterialAllocationError("本站已有谱系未出现在冻结输入中，不能猜测新谱系或父项")
                    else:
                        origin = {"kind": "new"}
                self._validate_origin(origin, source["input_material_refs"], local, revision, known=bool(known))
                proposals.append({"material_port": output["material_port"], "item_key": output["item_key"],
                                  "local_material_uuid": local, "local_revision": revision, "origin": origin})
                snapshots.append(self._snapshot(c, local))
            request = {"task_payload_digest": source["task_payload_digest"], "items": proposals}
            c.execute("""UPDATE station_material_export SET source_digest=?,source_json=?,request_json=?,snapshot_json=?
                WHERE invocation_uuid=?""", (digest(frozen), _json(frozen), _json(request), _json(snapshots), invocation))
            for local in identities:
                c.execute("INSERT INTO station_material_export_item(invocation_uuid,local_material_uuid) VALUES (?,?)", (invocation,local))
            self._clear_failure_in(c, invocation, "prepare")
            return self._intent(c.execute("SELECT * FROM station_material_export WHERE invocation_uuid=?", (invocation,)).fetchone())

    @staticmethod
    def _validate_origin(origin, input_refs, local, revision, *, known):
        if not isinstance(origin, dict):
            raise MaterialAllocationError("谱系来源必须为闭合对象")
        kind = origin.get("kind")
        parents = {r["material_lineage_uuid"]: r for r in input_refs}
        if kind == "new" and set(origin) == {"kind"}:
            if known or any(r["local_material_uuid"] == local for r in input_refs):
                raise MaterialAllocationError("已有物料身份不能重新声明为新物料")
            return
        if kind == "retained" and set(origin) == {"kind", "material_lineage_uuid"}:
            ref = parents.get(canonical_uuid(origin["material_lineage_uuid"]))
            if ref is None or ref["local_material_uuid"] != local or ref["local_revision"] > revision:
                raise MaterialAllocationError("保留谱系必须匹配冻结输入和非回退的本站修订")
            return
        if kind == "derived" and set(origin) == {"kind", "operation", "parents"}:
            ids = origin["parents"]
            operation = origin["operation"]
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 100 or operation not in {"split", "merge", "transform"}
                    or (operation == "split" and len(ids) != 1) or (operation == "merge" and len(ids) < 2)
                    or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or any(canonical_uuid(i) not in parents for i in ids)):
                raise MaterialAllocationError("分装、合并或转化的父项必须来自冻结输入")
            return
        raise MaterialAllocationError("谱系来源字段无效")

    def remember_response(self, invocation_uuid: str, response: dict[str, Any]) -> dict[str, Any]:
        """网络回执先写入库存库；工作流库失败不丢失已经分配的身份。"""
        with self.store.transaction() as c:
            row = c.execute("SELECT * FROM station_material_export WHERE invocation_uuid=?", (canonical_uuid(invocation_uuid),)).fetchone()
            if row is None or row["request_json"] == "null":
                raise MaterialAllocationError("谱系回执缺少已持久的库存申请")
            request = json.loads(row["request_json"])
            validated = validate_allocation_receipt(response, invocation_uuid=invocation_uuid,
                instance_uuid=self.instance_uuid, station_uuid=self.station_uuid, request=request)
            if row["response_json"] is not None and digest(json.loads(row["response_json"])) != digest(validated):
                raise MaterialAllocationError("中心谱系映射已冻结，禁止替换原回执")
            c.execute("UPDATE station_material_export SET response_json=? WHERE invocation_uuid=?", (_json(validated), invocation_uuid))
            for item in validated["items"]:
                ref = item["material_ref"]
                c.execute("UPDATE station_material_export_item SET material_lineage_uuid=? WHERE invocation_uuid=? AND local_material_uuid=?", (ref["material_lineage_uuid"], invocation_uuid, ref["local_material_uuid"]))
            return validated

    def record_boundary(self, invocation_uuid: str, containers: list[dict[str, Any]], *, handoff_authority: Any = None) -> dict[str, Any]:
        """映射及容器权威均已核实后落盘最终边界；库存变动保留待核实，不生成新谱系。"""
        from unilabos.center_protocol.events import validate_outcome_refs
        from unilabos.center_protocol.handoff import validate_container

        with self.store.transaction() as c:
            row = c.execute("SELECT * FROM station_material_export WHERE invocation_uuid=?", (canonical_uuid(invocation_uuid),)).fetchone()
            if row is None or row["response_json"] is None:
                raise MaterialAllocationError("物料边界缺少持久谱系映射")
            if row["evidence_json"] is not None:
                if digest(json.loads(row["containers_json"])) != digest(containers):
                    raise MaterialAllocationError("容器边界已经冻结，不能改写")
                return json.loads(row["evidence_json"])
            snapshots = json.loads(row["snapshot_json"])
            if any(digest(self._snapshot(c, snapshot["material"]["uuid"])) != digest(snapshot) for snapshot in snapshots):
                raise MaterialAllocationError("分配谱系期间库存事实已改变，等待核实，不能伪造旧快照")
            refs = [item["material_ref"] for item in json.loads(row["response_json"])["items"]]
            if refs:
                if handoff_authority is None or handoff_authority.store is not self.store:
                    raise MaterialAllocationError("容器核验与物料导出必须使用同一个库存权威")
                handoff_authority.verify_output_containers(c, refs, containers)
            covered, container_refs = set(), []
            for entry in containers:
                if set(entry) != {"container_uuid", "barcode_namespace", "barcode", "source_material_refs", "source_snapshot", "center_snapshot"}:
                    raise MaterialAllocationError("导出容器快照字段无效")
                state = validate_container(entry["center_snapshot"], entry["container_uuid"])
                if (state["current_station_uuid"] != self.station_uuid or state["barcode_namespace"] != entry["barcode_namespace"]
                        or state["barcode"] != entry["barcode"] or state["custody_status"] != "available"
                        or state["active_handoff_item_uuid"] is not None or state["attention"]):
                    raise MaterialAllocationError("容器当前归属、条码或可用状态与本站输出不符")
                for ref in entry["source_material_refs"]:
                    key = ref["local_material_uuid"]
                    if key in covered or not any({k: v for k, v in expected.items() if k != "handoff_item_uuid"} == ref for expected in refs):
                        raise MaterialAllocationError("容器快照包含重复或非输出物料")
                    covered.add(key)
                container_refs.append({"container_uuid": entry["container_uuid"], "state_version": state["state_version"]})
            if covered != {ref["local_material_uuid"] for ref in refs}:
                raise MaterialAllocationError("所有输出物料必须有明确且完整的容器边界")
            evidence = validate_outcome_refs({"material_refs": refs, "container_refs": container_refs, "artifact_refs": []}, station_uuid=self.station_uuid)
            c.execute("UPDATE station_material_export SET evidence_json=?,containers_json=?,last_error=NULL WHERE invocation_uuid=?", (_json(evidence), _json(containers), invocation_uuid))
            return evidence

    @staticmethod
    def _phase(phase: str) -> str:
        if phase not in {"prepare", "evidence", "boundary"}:
            raise ValueError("物料补报阶段无效")
        return phase

    def export_failure(self, invocation_uuid: str, phase: str) -> dict[str, Any] | None:
        """当前诊断与导出尝试同一行保存，准备失败和重启都不会丢失。"""
        invocation, phase = canonical_uuid(invocation_uuid), self._phase(phase)
        with self.store.read_connection() as c:
            row = c.execute("SELECT local_task_uuid,retry_json FROM station_material_export WHERE invocation_uuid=?",
                (invocation,)).fetchone()
            failure = json.loads(row["retry_json"]).get(phase) if row else None
            return {"invocation_uuid": invocation, "phase": phase,
                    "local_task_uuid": row["local_task_uuid"], **failure} if failure else None

    def export_failures(self) -> list[dict[str, Any]]:
        """读取原持久诊断与冻结来源；不推断或创建新的导出尝试。"""
        result = []
        with self.store.read_connection() as c:
            for row in c.execute("SELECT * FROM station_material_export ORDER BY invocation_uuid").fetchall():
                source = json.loads(row["source_json"])["source"]
                for phase, failure in sorted(json.loads(row["retry_json"]).items()):
                    result.append({"invocation_uuid": row["invocation_uuid"],
                        "local_task_uuid": row["local_task_uuid"], "phase": phase,
                        "source": source, **failure})
        return result

    def can_attempt_export(self, invocation_uuid: str, phase: str) -> bool:
        failure = self.export_failure(invocation_uuid, phase)
        return failure is None or (failure["status"] == "retrying"
            and failure["next_attempt_ms"] <= self._now_ms())

    def record_failure(self, invocation_uuid: str, reason: str, *, phase: str = "evidence",
                       local_task_uuid: str | None = None, retryable: bool = False,
                       retry_after_seconds: float | None = None, error_type: str = "",
                       error_code: str | None = None) -> dict[str, Any]:
        """每阶段最多自动尝试五次；只保留诊断，不清理原申请、谱系或物理事实。"""
        invocation, phase = canonical_uuid(invocation_uuid), self._phase(phase)
        if local_task_uuid is not None:
            canonical_uuid(local_task_uuid)
        with self.store.transaction() as c:
            row = c.execute("SELECT local_task_uuid,retry_json FROM station_material_export WHERE invocation_uuid=?", (invocation,)).fetchone()
            if row is None or (local_task_uuid is not None and row["local_task_uuid"] != local_task_uuid):
                raise MaterialAllocationError("失败诊断缺少匹配的持久导出尝试")
            failures = json.loads(row["retry_json"])
            attempts = failures.get(phase, {}).get("attempts", 0) + 1
            status = "blocked" if not retryable else ("exhausted" if attempts >= 5 else "retrying")
            next_attempt = None
            if status == "retrying":
                delay_ms = min(300_000, 2_000 * 2 ** (attempts - 1))
                now_ms = self._now_ms()
                if retry_after_seconds is not None:
                    # HTTP 等待值不受数据库 BIGINT 范围保护；不能让诊断自身溢出并终止桥。
                    if (not math.isfinite(retry_after_seconds)
                            or retry_after_seconds > (2**63 - 1 - now_ms) // 1000):
                        status = "blocked"
                        reason = "Retry-After 超出可持久化范围，等待核实；" + reason
                    else:
                        delay_ms = max(delay_ms, math.ceil(max(0, retry_after_seconds) * 1000))
                if status == "retrying":
                    next_attempt = now_ms + delay_ms
            failure = {"opened_at_ms": failures.get(phase, {}).get("opened_at_ms", self._now_ms()), "observed_at_ms": max(self._now_ms(), failures.get(phase, {}).get("observed_at_ms", 0) + 1), "status": status, "attempts": attempts, "next_attempt_ms": next_attempt,
                "error_type": error_type[:100], "error_code": str(error_code)[:100] if error_code else None,
                "last_error": reason[:500]}
            failures[phase] = failure
            c.execute("UPDATE station_material_export SET retry_json=?,last_error=? WHERE invocation_uuid=?",
                (_json(failures), failure["last_error"], invocation))
            return {"invocation_uuid": invocation, "phase": phase, "local_task_uuid": row["local_task_uuid"], **failure}

    @staticmethod
    def _clear_failure_in(c, invocation: str, phase: str) -> None:
        """成功与当前诊断清除同事务；其他阶段的失败及预算继续保留。"""
        row = c.execute("SELECT retry_json FROM station_material_export WHERE invocation_uuid=?", (invocation,)).fetchone()
        failures = json.loads(row["retry_json"])
        changed = failures.pop(phase, None)
        if changed or phase != "prepare":
            last_error = failures[sorted(failures)[0]]["last_error"] if failures else None
            c.execute("UPDATE station_material_export SET retry_json=?,last_error=? WHERE invocation_uuid=?",
                (_json(failures), last_error, invocation))

    def resume_export(self, invocation_uuid: str, phase: str) -> None:
        """修复原因后重启该阶段预算；不改写原申请、谱系或实际任务。"""
        invocation, phase = canonical_uuid(invocation_uuid), self._phase(phase)
        with self.store.transaction() as c:
            row = c.execute("SELECT retry_json FROM station_material_export WHERE invocation_uuid=?", (invocation,)).fetchone()
            failures = json.loads(row["retry_json"]) if row else {}
            if phase in failures:
                failures[phase].update(status="retrying", attempts=0, next_attempt_ms=0)
                c.execute("UPDATE station_material_export SET retry_json=? WHERE invocation_uuid=?", (_json(failures), invocation))

    def completed_boundaries(self, *, after_invocation_uuid: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """为后续物料 DAG 提供已冻结快照，不从私有数据库跨层查询。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("物料边界查询批量必须为 1 至 100")
        with self.store.read_connection() as c:
            return [{"invocation_uuid": row["invocation_uuid"], "containers": json.loads(row["containers_json"]),
                     "evidence": json.loads(row["evidence_json"])} for row in c.execute(
                "SELECT * FROM station_material_export WHERE completed=1 AND invocation_uuid>? ORDER BY invocation_uuid LIMIT ?", (after_invocation_uuid,limit)).fetchall()]

    @staticmethod
    def _snapshot(c, local):
        material = c.execute("SELECT * FROM material WHERE uuid=?", (local,)).fetchone()
        if material is None:
            raise MaterialAllocationError("真实输出物料记录已丢失")
        content = c.execute("SELECT * FROM container_content WHERE material_uuid=? AND deleted_at IS NULL", (local,)).fetchone()
        content = dict(content) if content else None
        chemical = None
        if content and content.get("reagent_info_uuid"):
            chemical = c.execute("SELECT * FROM reagent_info WHERE uuid=?", (content["reagent_info_uuid"],)).fetchone()
        return {"material": dict(material), "content": content, "chemical": dict(chemical) if chemical else None}

    @staticmethod
    def _boundary_request(row) -> dict[str, Any]:
        containers = json.loads(row["containers_json"])
        return {"task_payload_digest": json.loads(row["request_json"])["task_payload_digest"], "items": [
            {"container_uuid": entry["container_uuid"],
             "expected_state_version": entry["center_snapshot"]["state_version"],
             "source_material_refs": entry["source_material_refs"],
             "source_snapshot": entry["source_snapshot"]} for entry in containers]}

    def pending_boundaries(self, *, after_invocation_uuid: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """只读取已冻结的原快照；与 outcome 的完成标记独立，不阻止原结果补报。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("物料边界查询批量必须为 1 至 100")
        with self.store.read_connection() as c:
            rows = c.execute("""SELECT e.* FROM station_material_export e
                WHERE e.completed=1 AND e.containers_json<>'[]' AND e.invocation_uuid>?
                AND NOT EXISTS(SELECT 1 FROM station_material_boundary_receipt r WHERE r.invocation_uuid=e.invocation_uuid)
                ORDER BY e.invocation_uuid LIMIT ?""", (after_invocation_uuid, limit)).fetchall()
            return [{"invocation_uuid": row["invocation_uuid"], "local_task_uuid": row["local_task_uuid"],
                     "request": self._boundary_request(row)} for row in rows]

    def acknowledge_boundary(self, invocation_uuid: str, receipt: dict[str, Any]) -> None:
        """仅精确回执关闭补报责任；重复确认不改写原回执。"""
        from unilabos.center_protocol.material import validate_boundary_receipt

        with self.store.transaction() as c:
            row = c.execute("SELECT * FROM station_material_export WHERE invocation_uuid=? AND completed=1",
                (canonical_uuid(invocation_uuid),)).fetchone()
            if row is None or row["containers_json"] is None:
                raise MaterialAllocationError("容器边界尚未冻结，不能确认")
            validated = validate_boundary_receipt(receipt, invocation_uuid=invocation_uuid,
                instance_uuid=self.instance_uuid, request=self._boundary_request(row))
            c.execute("INSERT INTO station_material_boundary_receipt(invocation_uuid,receipt_json) VALUES(?,?) ON CONFLICT(invocation_uuid) DO NOTHING",
                (invocation_uuid, _json(validated)))
            self._clear_failure_in(c, invocation_uuid, "boundary")

    def pending_intents(self, *, limit: int = 100, after_invocation_uuid: str = "") -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("物料补报批量必须为 1 至 100")
        with self.store.read_connection() as c:
            return [self._intent(r) for r in c.execute("SELECT * FROM station_material_export WHERE completed=0 AND request_json<>'null' AND invocation_uuid>? ORDER BY invocation_uuid LIMIT ?", (after_invocation_uuid, limit)).fetchall()]

    def acknowledge(self, invocation_uuid: str) -> None:
        """只能在工作流证据事务成功后调用；丢失标记时重放相同证据即可。"""
        with self.store.transaction() as c:
            changed = c.execute("UPDATE station_material_export SET completed=1 WHERE invocation_uuid=? AND evidence_json IS NOT NULL", (canonical_uuid(invocation_uuid),)).rowcount
            if changed != 1:
                raise MaterialAllocationError("物料导出尚无持久证据，不能确认完成")
            self._clear_failure_in(c, invocation_uuid, "evidence")

    @staticmethod
    def _intent(row) -> dict[str, Any]:
        return {"invocation_uuid": row["invocation_uuid"], "local_task_uuid": row["local_task_uuid"],
                "source": json.loads(row["source_json"])["source"], "request": json.loads(row["request_json"]),
                "response": json.loads(row["response_json"]) if row["response_json"] else None,
                "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else None,
                "containers": json.loads(row["containers_json"]) if row["containers_json"] else None,
                "completed": bool(row["completed"]), "last_error": row["last_error"]}
