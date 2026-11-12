"""V1.0 容器内容物合同：试剂信息、试剂、样品、当前内容物、内容物历史、预留与来源绑定。

数量的唯一权威是 ``material_substance``（dev 的内容物行，设备上报与人工录入写同一行）；
V1 的试剂 / 样品 / 当前内容物是挂在某个 ``substance_uuid`` 上的角色（``material_content``），
只多存浓度、密度来源、样品编号等 V1 字段。所有数量写入都经 ``MaterialsService.put_data``，
因此 ``content_version``、``state_hash``、账本与容量校验和设备同步完全沿用 dev 路径。

试剂信息（V1 ``reagent-info``）就是 dev 的化学品定义（``chemical`` 表）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from uuid import uuid4

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.materials import MaterialDataWrite, MaterialPatch, MaterialSubstance
from unilabos.server.api.v1_fields import iso_to_ms, ms_to_iso
from unilabos.server.services.materials.chemicals import ChemicalError, ChemicalService, normalize_cas
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialsService,
    MaterialValidationError,
)
from unilabos.server.services.materials.store import MaterialsRepository
from unilabos.server.services.materials.quantity import convert_quantity, quantity_tolerance
from unilabos.server.services.materials.resource_contract import new_mutation

_UNSET: Any = object()
_PHYSICAL_STATES = {"solid", "liquid", "gas", "other", "unknown"}
_MAX_BATCH = 500


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _page(page: int, page_size: int, *, maximum: int = 500) -> tuple[int, int]:
    return max(1, int(page or 1)), min(maximum, max(1, int(page_size or 20)))


def _quantity(value: Any, field: str = "quantity") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MaterialValidationError(f"{field} must be a number") from None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        raise MaterialValidationError(f"{field} must be non-negative")
    return number


def _physical_state(value: Any) -> str:
    state = str(value or "").strip().lower() or "unknown"
    if state not in _PHYSICAL_STATES:
        raise MaterialValidationError("physical_state is invalid")
    # material_substance 只认 liquid/solid/gas/unknown；V1 的 other 归入 unknown
    return "unknown" if state == "other" else state


def _observed_ms(value: Any, fallback: int) -> int:
    if value in (None, ""):
        return fallback
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        result = iso_to_ms(str(value))
    except ValueError:
        raise MaterialValidationError("observed_at must be ISO 8601") from None
    return result if result is not None else fallback


def _concentration(values: Mapping[str, Any]) -> tuple[Optional[float], Optional[str]]:
    value, unit = values.get("concentration_value"), _text(values.get("concentration_unit"))
    if value is None and unit is None:
        return None, None
    if value is None or unit is None:
        raise MaterialValidationError("concentration_value and concentration_unit must be provided together")
    return _quantity(value, "concentration_value"), unit


class ContentContract:
    """V1 内容物合同；与 :class:`MaterialsService` 共用 materials.db 连接与写锁。"""

    def __init__(
        self,
        materials: MaterialsService,
        *,
        chemicals: Optional[ChemicalService] = None,
        compound_source: Any = _UNSET,
    ) -> None:
        self.materials = materials
        self.chemicals = chemicals or ChemicalService(materials)
        if compound_source is _UNSET:
            from unilabos.server.services.materials.compound_source import configured_compound_source

            compound_source = configured_compound_source()
        self.compound_source = compound_source

    @property
    def connection(self) -> Any:
        return self.materials.connection

    def _now(self) -> int:
        return self.materials._now_ms()

    # ── 试剂信息（化学品定义） ───────────────────────────────────

    @staticmethod
    def reagent_info_row(chemical: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "uuid": chemical["chemical_uuid"],
            "create_time": ms_to_iso(chemical["created_at_ms"]),
            "update_time": ms_to_iso(chemical["updated_at_ms"]),
            "description": chemical.get("description") or None,
            "meta_data": dict(chemical.get("meta_data") or {}),
            "cas": chemical.get("cas"),
            "name": chemical["name"],
            "name_en": chemical.get("name_en"),
            "aliases": list(chemical.get("aliases") or []),
            "molecular_formula": chemical.get("molecular_formula"),
            "smiles": chemical.get("smiles"),
            "inchi_key": chemical.get("inchi_key"),
            "molecular_weight": chemical.get("molecular_weight"),
            "density_g_per_ml": chemical.get("density_g_per_ml"),
            "physical_state": chemical.get("physical_state") or "unknown",
            "version": chemical.get("version"),
        }

    @staticmethod
    def _chemical_values(values: Mapping[str, Any], *, partial: bool) -> dict[str, Any]:
        allowed = ("name", "name_en", "aliases", "cas", "molecular_formula", "smiles", "inchi_key",
                   "molecular_weight", "density_g_per_ml", "physical_state", "description", "meta_data")
        result = {key: values[key] for key in allowed if key in values and (not partial or values[key] is not None or key in ("name_en", "molecular_formula", "smiles", "inchi_key", "molecular_weight", "density_g_per_ml", "description"))}
        if "physical_state" in result:
            result["physical_state"] = _physical_state(result["physical_state"])
        if "description" in result and result["description"] is None:
            result["description"] = ""
        return result

    def create_reagent_info(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return self.reagent_info_row(self.chemicals.create_chemical(self._chemical_values(values, partial=False)))

    def create_reagent_infos(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """整批原子登记：任何一行失败整批回滚。"""

        with self.materials.write():
            return [self.create_reagent_info(row) for row in rows]

    def list_reagent_infos(self, *, page: int = 0, page_size: int = 0, name: str = "", cas: str = "",
                           physical_state: str = "") -> dict[str, Any]:
        page, page_size = _page(page, page_size)
        result = self.chemicals.list_chemicals(q=name, cas=cas, physical_state=physical_state, page=page, page_size=page_size)
        total = int(result["total"])
        return {"items": [self.reagent_info_row(item) for item in result["items"]], "total": total,
                "page": page, "page_size": page_size, "has_more": page * page_size < total}

    def get_reagent_info(self, identity: str) -> dict[str, Any]:
        return self.reagent_info_row(self.chemicals.get_chemical(identity))

    def update_reagent_info(self, identity: str, values: Mapping[str, Any]) -> dict[str, Any]:
        current = self.chemicals.get_chemical(identity)
        data = self._chemical_values(values, partial=True)
        if not data:
            return self.reagent_info_row(current)
        return self.reagent_info_row(self.chemicals.update_chemical(identity, data, expected_version=current["version"]))

    def delete_reagent_info(self, identity: str) -> None:
        current = self.chemicals.get_chemical(identity)
        used = self.connection.execute(
            "SELECT 1 FROM material_substance JOIN material USING(material_uuid) "
            "WHERE material_substance.chemical_uuid=? AND material.deleted_at_ms IS NULL LIMIT 1",
            (identity,),
        ).fetchone()
        if used:
            raise ChemicalError("in_use", "reagent identity is still referenced by a reagent")
        self.chemicals.delete_chemical(identity, expected_version=current["version"])

    def get_reagent_info_structure(self, identity: str) -> dict[str, Any]:
        """化学品三维结构缓存：本地尚无生成器，按 V1 合同返回 pending 投影。"""

        chemical = self.chemicals.get_chemical(identity)
        structure = _mapping(_mapping(chemical.get("meta_data")).get("structure_3d"))
        return {
            "reagent_info_uuid": identity,
            "identity_key": structure.get("identity_key") or (chemical.get("inchi_key") or ""),
            "format": structure.get("format") or "",
            "source": structure.get("source") or "",
            "source_id": structure.get("source_id"),
            "content": structure.get("content"),
            "checksum": structure.get("checksum"),
            "status": structure.get("status") or "pending",
            "generated_at": structure.get("generated_at"),
            "error_message": structure.get("error_message"),
            "update_time": ms_to_iso(chemical["updated_at_ms"]),
        }

    def lookup_compound(self, cas_value: str) -> dict[str, Any]:
        """按 CAS 查本地登记；未登记时可选查询外部数据源（PubChem），只回候选不写库。"""

        try:
            cas = normalize_cas(cas_value)
        except ChemicalError as error:
            raise MaterialValidationError(error.message) from None
        found = self.connection.execute(
            "SELECT 1 FROM chemical WHERE cas=? AND deleted_at_ms IS NULL", (cas,)
        ).fetchone()
        if found:
            return {"cas": cas, "status": "registered", "message": "该 CAS 已在本地试剂身份目录登记"}
        if self.compound_source is None:
            return {"cas": cas, "status": "unavailable", "message": "未配置外部化合物数据源"}
        try:
            compound = self.compound_source.lookup_by_cas(cas)
        except LookupError:
            return {"cas": cas, "status": "not_found", "message": "化合物数据源没有收录该 CAS，请手工填写化学信息"}
        except Exception:  # noqa: BLE001 - 外部数据源故障不得阻断试剂录入
            return {"cas": cas, "status": "unavailable", "message": "化合物数据源暂时不可用，请手工填写化学信息"}
        return {"cas": cas, "status": "ok", "compound": compound}

    # ── 内容物写入（全部经 put_data） ─────────────────────────────

    def _substances(self, material_uuid: str) -> tuple[Any, list[MaterialSubstance]]:
        aggregate = self.materials.get_material(material_uuid)
        substances = [
            MaterialSubstance.model_validate({**item.model_dump(mode="json"), "chemical": None})
            for item in aggregate.data.substances
        ]
        return aggregate, substances

    def _put_substances(self, material_uuid: str, substances: list[MaterialSubstance], *,
                        observed_at_ms: Optional[int] = None, job_uuid: Optional[str] = None) -> Any:
        aggregate = self.materials.get_material(material_uuid)
        data = aggregate.data
        value = MaterialDataWrite(
            data=dict(data.data), substances=substances, sites_initialized=data.sites_initialized,
            unknown_counter=data.unknown_counter, state_status=data.state_status,
            observed_at_ms=max(observed_at_ms or 0, data.observed_at_ms),
        )
        mutation = new_mutation("put_data")
        if job_uuid:
            # 作业结算产生的内容物变化在账本上带作业身份（任务物料变化 / 消费读模型按作业筛选）
            mutation = mutation.model_copy(update={"job_uuid": job_uuid, "actor_type": "workflow"})
        try:
            return self.materials.put_data(mutation, material_uuid, value).data
        except MaterialNoChangeError:
            return aggregate

    def _set_capacity(self, material_uuid: str, capacity: Any, expected_revision: Any) -> None:
        from unilabos.server.services.materials.capacity import normalize_capacity

        material = self.materials.get_material(material_uuid).material
        if expected_revision is not None and int(expected_revision) != material.version:
            raise MaterialConflictError("Material revision has changed")
        try:
            normalized = normalize_capacity(capacity)
        except ValueError as error:
            raise MaterialValidationError(str(error)) from None
        patch = MaterialPatch(config={**material.config, "capacity": normalized})
        try:
            self.materials.patch_material(new_mutation("patch_material"), material_uuid, patch)
        except MaterialNoChangeError:
            pass

    def _content(self, substance_uuid: str, kind: str) -> Any:
        row = self.connection.execute(
            "SELECT * FROM material_content WHERE substance_uuid=? AND content_kind=?", (substance_uuid, kind)
        ).fetchone()
        if row is None:
            raise MaterialNotFoundError(f"{ {'reagent': 'reagent', 'sample': 'sample', 'current': 'current substance'}[kind]} does not exist")
        return row

    def _content_of_material(self, material_uuid: str, kind: str) -> Any:
        return self.connection.execute(
            "SELECT * FROM material_content WHERE material_uuid=? AND content_kind=?", (material_uuid, kind)
        ).fetchone()

    def _insert_content(self, substance_uuid: str, material_uuid: str, kind: str, now: int, **fields: Any) -> None:
        columns = ["substance_uuid", "material_uuid", "content_kind", "created_at_ms", "updated_at_ms", *fields]
        values = [substance_uuid, material_uuid, kind, now, now, *fields.values()]
        self.connection.execute(
            f"INSERT INTO material_content({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values
        )

    def _update_content(self, substance_uuid: str, now: int, **fields: Any) -> None:
        assignments = ",".join(f"{key}=?" for key in fields)
        self.connection.execute(
            f"UPDATE material_content SET {assignments}{',' if assignments else ''} revision=revision+1, "
            "updated_at_ms=MAX(?, created_at_ms) WHERE substance_uuid=?",
            (*fields.values(), now, substance_uuid),
        )

    def _append_history(self, *, material_uuid: str, subject_type: str, subject_uuid: str, event_type: str,
                        quantity_delta: Optional[float], quantity_unit: Optional[str], revision: Optional[int],
                        previous: Optional[Mapping[str, Any]], result: Optional[Mapping[str, Any]],
                        extension: Optional[Mapping[str, Any]] = None, operator_type: str = "user",
                        causation_id: Optional[str] = None, task_uuid: Optional[str] = None,
                        job_uuid: Optional[str] = None) -> str:
        history_uuid = str(uuid4())
        self.connection.execute(
            "INSERT INTO material_content_history(history_uuid, material_uuid, subject_type, subject_uuid, event_type, "
            "operator_type, quantity_delta, quantity_unit, revision, changes_json, extension_json, workflow_task_uuid, "
            "workflow_node_job_uuid, trace_id, causation_id, recorded_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (history_uuid, material_uuid, subject_type, subject_uuid, event_type, operator_type, quantity_delta,
             quantity_unit, revision, json.dumps({"previous": previous, "result": result}, ensure_ascii=False),
             json.dumps(dict(extension or {}), ensure_ascii=False), task_uuid, job_uuid, None,
             causation_id or "", self._now()),
        )
        return history_uuid

    def _reserved(self, substance_uuid: str, unit: str, *, exclude_task: Optional[str] = None) -> float:
        total = 0.0
        for row in self.connection.execute(
            "SELECT quantity, quantity_unit, task_uuid FROM material_content_reservation "
            "WHERE substance_uuid=? AND status='active'", (substance_uuid,),
        ).fetchall():
            if exclude_task is not None and row["task_uuid"] == exclude_task:
                continue
            converted = convert_quantity(float(row["quantity"]), row["quantity_unit"], unit, inventory_type="content")
            total += converted[0] if converted else float(row["quantity"])
        return total

    def _assert_not_below_reserved(self, substance_uuid: str, quantity: float, unit: str) -> None:
        reserved = self._reserved(substance_uuid, unit)
        if quantity + quantity_tolerance(quantity, reserved) < reserved:
            raise MaterialConflictError(
                f"quantity {quantity:g} {unit} is below the active workflow reservation {reserved:g} {unit}",
                conflict="state",
            )

    # ── 试剂 ──────────────────────────────────────────────────

    _REAGENT_SELECT = (
        "SELECT mc.*, ms.material_uuid AS ms_material_uuid, ms.name AS substance_name, ms.quantity, ms.quantity_unit, "
        "ms.physical_state, ms.chemical_uuid, ms.meta_data_json, ms.composition_json, ms.observed_at_ms, "
        "m.barcode AS container_barcode, m.name AS container_name, m.version AS material_revision, "
        "m.config_json, m.template_uuid, c.name AS chemical_name, c.name_en, c.aliases_json, c.cas, "
        "c.molecular_formula, c.smiles, c.inchi_key, c.molecular_weight, "
        "c.density_g_per_ml AS chemical_density FROM material_content mc "
        "JOIN material_substance ms ON ms.substance_uuid = mc.substance_uuid "
        "JOIN material m ON m.material_uuid = mc.material_uuid "
        "LEFT JOIN chemical c ON c.chemical_uuid = ms.chemical_uuid"
    )

    def _capacity_fields(self, row: Any) -> dict[str, Any]:
        from unilabos.server.services.materials.capacity import capacity_projection

        config = json.loads(row["config_json"] or "{}")
        try:
            template = self.materials.get_template(row["template_uuid"])
            capacity = capacity_projection(config, template.definition or {})
        except Exception:  # noqa: BLE001 - 容量是展示投影，模板缺失时按无上限
            capacity = {}
        return {"container_capacity": capacity or None, "configured_capacity": config.get("capacity"),
                "rated_capacity": capacity or None, "maximum_capacity": capacity or None}

    def _reagent_row(self, row: Any) -> dict[str, Any]:
        meta = json.loads(row["meta_data_json"] or "{}")
        result = {
            "uuid": row["substance_uuid"],
            "create_time": ms_to_iso(row["created_at_ms"]),
            "update_time": ms_to_iso(row["updated_at_ms"]),
            "description": row["description"] or None,
            "meta_data": meta,
            "material_uuid": row["material_uuid"],
            "reagent_info_uuid": row["chemical_uuid"],
            "concentration_value": row["concentration_value"],
            "concentration_unit": row["concentration_unit"],
            "quantity": row["quantity"],
            "quantity_unit": row["quantity_unit"],
            "revision": row["revision"],
            "physical_state": row["physical_state"],
            "density_g_per_ml": row["density_g_per_ml"] if row["density_g_per_ml"] is not None else row["chemical_density"],
            "density_source": row["density_source"] or ("reagent_info" if row["chemical_density"] is not None else None),
            "name": row["chemical_name"] or row["substance_name"],
            "name_en": row["name_en"],
            "aliases": json.loads(row["aliases_json"] or "[]"),
            "cas": row["cas"],
            "molecular_formula": row["molecular_formula"],
            "smiles": row["smiles"],
            "inchi_key": row["inchi_key"],
            "molecular_weight": row["molecular_weight"],
            "container_barcode": row["container_barcode"],
            "container_name": row["container_name"],
            "material_revision": row["material_revision"],
            "active_workflow_reserved_quantity": self._reserved(row["substance_uuid"], row["quantity_unit"]),
        }
        result.update(self._capacity_fields(row))
        return result

    def _reagent_state(self, substance_uuid: str) -> dict[str, Any]:
        row = self._reagent_query("mc.substance_uuid=?", (substance_uuid,))
        if not row:
            return {}
        item = self._reagent_row(row[0])
        return {key: item.get(key) for key in (
            "quantity", "quantity_unit", "concentration_value", "concentration_unit", "physical_state",
            "density_g_per_ml", "revision", "description", "meta_data", "container_capacity", "maximum_capacity",
        )}

    def _reagent_query(self, clause: str, params: Sequence[Any], *, suffix: str = "") -> list[Any]:
        return self.connection.execute(
            f"{self._REAGENT_SELECT} WHERE mc.content_kind='reagent' AND m.deleted_at_ms IS NULL AND {clause} {suffix}",
            tuple(params),
        ).fetchall()

    def _resolve_chemical(self, values: Mapping[str, Any]) -> dict[str, Any]:
        identity = _text(values.get("reagent_info_uuid"))
        if identity:
            return self.chemicals.get_chemical(identity)
        cas = _text(values.get("cas"))
        if not cas:
            raise MaterialValidationError("reagent_info_uuid or cas is required")
        try:
            cas = normalize_cas(cas)
        except ChemicalError as error:
            raise MaterialValidationError(error.message) from None
        row = self.connection.execute(
            "SELECT chemical_uuid FROM chemical WHERE cas=? AND deleted_at_ms IS NULL", (cas,)
        ).fetchone()
        if row is None:
            raise ChemicalError("not_found", f"reagent identity with CAS {cas} does not exist")
        return self.chemicals.get_chemical(row[0])

    def create_reagent(self, values: Mapping[str, Any], *, extension: Optional[Mapping[str, Any]] = None,
                       causation_id: Optional[str] = None) -> dict[str, Any]:
        """为一个容器登记试剂（一个容器至多一份活动试剂）；数量写入该容器的内容物。"""

        material_uuid = _text(values.get("material_uuid"))
        if not material_uuid:
            raise MaterialValidationError("material_uuid is required")
        quantity = _quantity(values.get("quantity"))
        unit = _text(values.get("quantity_unit"))
        if not unit:
            raise MaterialValidationError("quantity_unit is required")
        concentration, concentration_unit = _concentration(values)
        with self.materials.write():
            _, substances = self._substances(material_uuid)
            chemical = self._resolve_chemical(values)
            if self._content_of_material(material_uuid, "reagent") is not None:
                raise MaterialConflictError("container already holds an active reagent")
            if values.get("container_capacity") is not None:
                self._set_capacity(material_uuid, values["container_capacity"], values.get("expected_material_revision"))
            state = _physical_state(values.get("physical_state"))
            if state == "unknown":
                state = chemical.get("physical_state") or "unknown"
            now = self._now()
            meta = _mapping(values.get("meta_data"))
            if _text(values.get("source")):
                meta["source"] = values["source"]
            substance_uuid = str(uuid4())
            substances.append(MaterialSubstance(
                substance_uuid=substance_uuid, name=chemical["name"], quantity=quantity, quantity_unit=unit,
                physical_state=state, meta_data=meta, chemical_uuid=chemical["chemical_uuid"],
            ))
            self._put_substances(material_uuid, substances, observed_at_ms=_observed_ms(values.get("observed_at"), now))
            density = values.get("density_g_per_ml")
            self._insert_content(
                substance_uuid, material_uuid, "reagent", now,
                concentration_value=concentration, concentration_unit=concentration_unit,
                density_g_per_ml=float(density) if density is not None else None,
                density_source="manual" if density is not None else None,
                description=str(values.get("description") or ""),
            )
            result = self._reagent_state(substance_uuid)
            self._append_history(
                material_uuid=material_uuid, subject_type="reagent", subject_uuid=substance_uuid,
                event_type="create", quantity_delta=quantity, quantity_unit=unit, revision=1, previous=None,
                result=result, extension={"source": values.get("source"), **dict(extension or {})},
                causation_id=causation_id,
            )
        return self.get_reagent(substance_uuid)

    def create_reagents(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        with self.materials.write():
            return [self.create_reagent(row) for row in rows]

    def list_reagents(self, *, page: int = 0, page_size: int = 0, material_uuid: str = "", reagent_info_uuid: str = "",
                      keyword: str = "", cas: str = "", barcode: str = "") -> dict[str, Any]:
        page, page_size = _page(page, page_size)
        clauses, params = ["(c.deleted_at_ms IS NULL OR c.chemical_uuid IS NULL)"], []
        for column, value in (("mc.material_uuid", material_uuid), ("ms.chemical_uuid", reagent_info_uuid),
                              ("c.cas", cas), ("m.barcode", barcode)):
            if value.strip():
                clauses.append(f"LOWER({column})=LOWER(?)")
                params.append(value.strip())
        if keyword.strip():
            clauses.append("(LOWER(COALESCE(c.name, ms.name)) LIKE LOWER(?) OR LOWER(m.name) LIKE LOWER(?))")
            params.extend([f"%{keyword.strip()}%"] * 2)
        clause = " AND ".join(clauses)
        total = len(self._reagent_query(clause, params))
        rows = self._reagent_query(clause, params, suffix=f"ORDER BY LOWER(COALESCE(c.name, ms.name)), mc.substance_uuid LIMIT {page_size} OFFSET {(page - 1) * page_size}")
        return {"items": [self._reagent_row(row) for row in rows], "total": total, "page": page,
                "page_size": page_size, "has_more": page * page_size < total}

    def get_reagent(self, identity: str) -> dict[str, Any]:
        rows = self._reagent_query("mc.substance_uuid=?", (identity,))
        if not rows:
            raise MaterialNotFoundError("reagent does not exist")
        return self._reagent_row(rows[0])

    def update_reagent(self, identity: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """人工校正试剂数量 / 浓度 / 描述；不能低于活动工作流预留量。"""

        with self.materials.write():
            content = self._content(identity, "reagent")
            material_uuid = content["material_uuid"]
            previous = self._reagent_state(identity)
            expected = values.get("expected_revision")
            if expected is not None and int(expected) != int(content["revision"]):
                raise MaterialConflictError("Reagent revision has changed")
            if values.get("container_capacity") is not None:
                self._set_capacity(material_uuid, values["container_capacity"], values.get("expected_material_revision"))
            _, substances = self._substances(material_uuid)
            target = next(item for item in substances if item.substance_uuid == identity)
            quantity = _quantity(values.get("quantity", target.quantity))
            unit = _text(values.get("quantity_unit")) or target.quantity_unit
            self._assert_not_below_reserved(identity, quantity, unit)
            meta = dict(target.meta_data)
            if "meta_data" in values and values["meta_data"] is not None:
                meta = {**meta, **_mapping(values["meta_data"])}
            if _text(values.get("source")):
                meta["source"] = values["source"]
            updated = target.model_copy(update={"quantity": quantity, "quantity_unit": unit, "meta_data": meta})
            now = self._now()
            self._put_substances(
                material_uuid, [updated if item.substance_uuid == identity else item for item in substances],
                observed_at_ms=_observed_ms(values.get("observed_at"), now),
            )
            fields: dict[str, Any] = {}
            if "concentration_value" in values or "concentration_unit" in values:
                fields["concentration_value"], fields["concentration_unit"] = _concentration(values)
            if "description" in values:
                fields["description"] = str(values.get("description") or "")
            self._update_content(identity, now, **fields)
            result = self._reagent_state(identity)
            converted = convert_quantity(target.quantity, target.quantity_unit, unit, inventory_type="content")
            delta = quantity - (converted[0] if converted else target.quantity)
            self._append_history(
                material_uuid=material_uuid, subject_type="reagent", subject_uuid=identity, event_type="adjust",
                quantity_delta=delta, quantity_unit=unit, revision=result.get("revision"), previous=previous,
                result=result, extension={"source": values.get("source")},
            )
        return self.get_reagent(identity)

    def delete_reagent(self, identity: str) -> None:
        with self.materials.write():
            content = self._content(identity, "reagent")
            material_uuid = content["material_uuid"]
            if self._reserved(identity, "ul") > 0 or self.connection.execute(
                "SELECT 1 FROM material_content_reservation WHERE substance_uuid=? AND status='active'", (identity,)
            ).fetchone():
                raise MaterialConflictError("reagent is reserved by an active workflow", conflict="state")
            previous = self._reagent_state(identity)
            _, substances = self._substances(material_uuid)
            self._put_substances(material_uuid, [item for item in substances if item.substance_uuid != identity])
            self._append_history(
                material_uuid=material_uuid, subject_type="reagent", subject_uuid=identity, event_type="delete",
                quantity_delta=-float(previous.get("quantity") or 0), quantity_unit=previous.get("quantity_unit"),
                revision=previous.get("revision"), previous=previous, result=None,
            )

    def dispense_reagent(self, payload: Mapping[str, Any], *, command_id: str, actor: str = "") -> dict[str, Any]:
        """把一份源试剂原子分装到若干空容器（V1 ``reagent.dispense``）。"""

        source_uuid = _text(payload.get("source_reagent_uuid"))
        unit = _text(payload.get("quantity_unit"))
        targets = list(payload.get("targets") or [])
        if not source_uuid or not unit or not targets:
            raise MaterialValidationError("source_reagent_uuid, quantity_unit and targets are required")
        if len({str(_mapping(item).get("material_uuid")) for item in targets}) != len(targets):
            raise MaterialValidationError("dispense targets must be distinct containers")
        with self.materials.write():
            content = self._content(source_uuid, "reagent")
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(content["revision"]):
                raise MaterialConflictError("Reagent revision has changed")
            source = self.get_reagent(source_uuid)
            total = sum(_quantity(_mapping(item).get("quantity")) for item in targets)
            converted = convert_quantity(total, unit, source["quantity_unit"], inventory_type="content",
                                         physical_state=source["physical_state"],
                                         concentration_value=source["concentration_value"],
                                         density_g_per_ml=source["density_g_per_ml"])
            if converted is None:
                raise MaterialValidationError("dispense unit is incompatible with the source reagent")
            remaining = float(source["quantity"]) - converted[0]
            available = float(source["quantity"]) - self._reserved(source_uuid, source["quantity_unit"])
            if converted[0] > available + quantity_tolerance(available, converted[0]):
                raise MaterialConflictError("source reagent quantity is insufficient", conflict="state")
            self.update_reagent(source_uuid, {"quantity": max(0.0, remaining), "quantity_unit": source["quantity_unit"],
                                              "source": actor or "inventory.command"})
            created = []
            for item in targets:
                target = _mapping(item)
                reagent = self.create_reagent(
                    {
                        "material_uuid": target.get("material_uuid"), "reagent_info_uuid": source["reagent_info_uuid"],
                        "quantity": target.get("quantity"), "quantity_unit": unit,
                        "physical_state": source["physical_state"],
                        "concentration_value": source["concentration_value"],
                        "concentration_unit": source["concentration_unit"],
                        "container_capacity": target.get("container_capacity"),
                        "expected_material_revision": target.get("expected_material_revision"),
                        "meta_data": {"source_reagent_uuid": source_uuid, "dispense_command_id": command_id},
                        "source": actor or "inventory.command",
                    },
                    extension={"source_reagent_uuid": source_uuid}, causation_id=command_id,
                )
                created.append(reagent)
            final = self.get_reagent(source_uuid)
        return {
            "source": {"reagent_uuid": source_uuid, "material_uuid": final["material_uuid"], "quantity": final["quantity"],
                       "quantity_unit": final["quantity_unit"], "revision": final["revision"]},
            "targets": [{"material_uuid": item["material_uuid"], "reagent_uuid": item["uuid"], "quantity": item["quantity"],
                         "quantity_unit": item["quantity_unit"], "revision": item["revision"]} for item in created],
        }

    # ── 样品 ──────────────────────────────────────────────────

    def _sample_row(self, row: Any) -> dict[str, Any]:
        return {
            "uuid": row["substance_uuid"],
            "create_time": ms_to_iso(row["created_at_ms"]),
            "update_time": ms_to_iso(row["updated_at_ms"]),
            "description": row["description"] or None,
            "meta_data": json.loads(row["meta_data_json"] or "{}"),
            "material_uuid": row["material_uuid"],
            "code": row["code"],
            "name": row["substance_name"],
            "quantity": row["quantity"],
            "quantity_unit": row["quantity_unit"],
            "revision": row["revision"],
            "container_barcode": row["container_barcode"],
            "container_name": row["container_name"],
        }

    def _sample_query(self, clause: str, params: Sequence[Any], *, suffix: str = "") -> list[Any]:
        return self.connection.execute(
            f"{self._REAGENT_SELECT} WHERE mc.content_kind='sample' AND m.deleted_at_ms IS NULL AND {clause} {suffix}",
            tuple(params),
        ).fetchall()

    def _sample_state(self, identity: str) -> dict[str, Any]:
        rows = self._sample_query("mc.substance_uuid=?", (identity,))
        return {key: self._sample_row(rows[0])[key] for key in ("code", "name", "quantity", "quantity_unit", "revision", "description", "meta_data")} if rows else {}

    def _check_sample_code(self, code: str, *, exclude: Optional[str] = None) -> None:
        row = self.connection.execute(
            "SELECT substance_uuid FROM material_content WHERE LOWER(code)=LOWER(?)", (code,)
        ).fetchone()
        if row is not None and row[0] != exclude:
            raise MaterialConflictError("sample code already exists", conflict="identity")

    def create_sample(self, values: Mapping[str, Any]) -> dict[str, Any]:
        material_uuid, code, name = (_text(values.get(key)) for key in ("material_uuid", "code", "name"))
        unit = _text(values.get("quantity_unit"))
        if not (material_uuid and code and name and unit):
            raise MaterialValidationError("material_uuid, code, name and quantity_unit are required")
        quantity = _quantity(values.get("quantity"))
        with self.materials.write():
            _, substances = self._substances(material_uuid)
            if self._content_of_material(material_uuid, "sample") is not None:
                raise MaterialConflictError("container already holds an active sample")
            self._check_sample_code(code)
            now = self._now()
            substance_uuid = str(uuid4())
            substances.append(MaterialSubstance(substance_uuid=substance_uuid, name=name, quantity=quantity,
                                                quantity_unit=unit, physical_state="unknown",
                                                meta_data=_mapping(values.get("meta_data"))))
            self._put_substances(material_uuid, substances)
            self._insert_content(substance_uuid, material_uuid, "sample", now, code=code,
                                 sample_type=_text(values.get("sample_type")), source=_text(values.get("source")),
                                 description=str(values.get("description") or ""))
            self._append_history(material_uuid=material_uuid, subject_type="sample", subject_uuid=substance_uuid,
                                 event_type="create", quantity_delta=quantity, quantity_unit=unit, revision=1,
                                 previous=None, result=self._sample_state(substance_uuid))
        return self.get_sample(substance_uuid)

    def list_samples(self, *, page: int = 0, page_size: int = 0, material_uuid: str = "", code: str = "",
                     name: str = "", keyword: str = "", barcode: str = "") -> dict[str, Any]:
        page, page_size = _page(page, page_size)
        clauses, params = ["1=1"], []
        if material_uuid.strip():
            clauses.append("mc.material_uuid=?")
            params.append(material_uuid.strip())
        for column, value in (("mc.code", code), ("ms.name", name)):
            if value.strip():
                clauses.append(f"LOWER({column}) LIKE LOWER(?)")
                params.append(f"%{value.strip()}%")
        if keyword.strip():
            clauses.append("(LOWER(mc.code) LIKE LOWER(?) OR LOWER(ms.name) LIKE LOWER(?) OR LOWER(m.barcode) LIKE LOWER(?))")
            params.extend([f"%{keyword.strip()}%"] * 3)
        if barcode.strip():
            clauses.append("LOWER(m.barcode)=LOWER(?)")
            params.append(barcode.strip())
        clause = " AND ".join(clauses)
        total = len(self._sample_query(clause, params))
        rows = self._sample_query(clause, params, suffix=f"ORDER BY mc.updated_at_ms DESC, mc.substance_uuid DESC LIMIT {page_size} OFFSET {(page - 1) * page_size}")
        return {"items": [self._sample_row(row) for row in rows], "total": total, "page": page,
                "page_size": page_size, "has_more": page * page_size < total}

    def get_sample(self, identity: str) -> dict[str, Any]:
        rows = self._sample_query("mc.substance_uuid=?", (identity,))
        if not rows:
            raise MaterialNotFoundError("sample does not exist")
        return self._sample_row(rows[0])

    def update_sample(self, identity: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self.materials.write():
            content = self._content(identity, "sample")
            material_uuid = content["material_uuid"]
            if _text(values.get("material_uuid")) and values["material_uuid"] != material_uuid:
                raise MaterialConflictError("sample container cannot be changed")
            previous = self._sample_state(identity)
            _, substances = self._substances(material_uuid)
            target = next(item for item in substances if item.substance_uuid == identity)
            quantity = _quantity(values.get("quantity", target.quantity))
            unit = _text(values.get("quantity_unit")) or target.quantity_unit
            self._assert_not_below_reserved(identity, quantity, unit)
            code = _text(values.get("code")) or content["code"]
            self._check_sample_code(code, exclude=identity)
            updated = target.model_copy(update={
                "name": _text(values.get("name")) or target.name, "quantity": quantity, "quantity_unit": unit,
                "meta_data": _mapping(values.get("meta_data")) if values.get("meta_data") is not None else target.meta_data,
            })
            self._put_substances(material_uuid, [updated if item.substance_uuid == identity else item for item in substances])
            fields: dict[str, Any] = {"code": code}
            if "description" in values:
                fields["description"] = str(values.get("description") or "")
            self._update_content(identity, self._now(), **fields)
            result = self._sample_state(identity)
            self._append_history(material_uuid=material_uuid, subject_type="sample", subject_uuid=identity,
                                 event_type="adjust", quantity_delta=quantity - target.quantity, quantity_unit=unit,
                                 revision=result.get("revision"), previous=previous, result=result)
        return self.get_sample(identity)

    def delete_sample(self, identity: str) -> None:
        self._delete_role(identity, "sample", "sample")

    def _delete_role(self, identity: str, kind: str, subject_type: str) -> None:
        with self.materials.write():
            content = self._content(identity, kind)
            material_uuid = content["material_uuid"]
            if self.connection.execute(
                "SELECT 1 FROM material_content_reservation WHERE substance_uuid=? AND status='active'", (identity,)
            ).fetchone():
                raise MaterialConflictError(f"{subject_type} is reserved by an active workflow", conflict="state")
            _, substances = self._substances(material_uuid)
            target = next((item for item in substances if item.substance_uuid == identity), None)
            self._put_substances(material_uuid, [item for item in substances if item.substance_uuid != identity])
            self._append_history(material_uuid=material_uuid, subject_type=subject_type, subject_uuid=identity,
                                 event_type="delete", quantity_delta=-(target.quantity if target else 0),
                                 quantity_unit=target.quantity_unit if target else None,
                                 revision=content["revision"], previous=None, result=None)

    # ── 当前内容物 ────────────────────────────────────────────

    def _current_row(self, row: Any) -> dict[str, Any]:
        composition = json.loads(row["composition_json"] or "[]")
        return {
            "uuid": row["substance_uuid"],
            "create_time": ms_to_iso(row["created_at_ms"]),
            "update_time": ms_to_iso(row["updated_at_ms"]),
            "description": row["description"] or None,
            "meta_data": json.loads(row["meta_data_json"] or "{}"),
            "material_uuid": row["material_uuid"],
            "name": row["substance_name"],
            "composition": composition,
            "components": [
                {**_mapping(item), "sort_order": index} for index, item in enumerate(composition)
            ],
            "quantity": row["quantity"],
            "quantity_unit": row["quantity_unit"],
            "physical_state": row["physical_state"],
            "revision": row["revision"],
            "observed_at": ms_to_iso(row["observed_at_ms"]),
        }

    def _current_query(self, clause: str, params: Sequence[Any], *, suffix: str = "") -> list[Any]:
        return self.connection.execute(
            f"{self._REAGENT_SELECT} WHERE mc.content_kind='current' AND m.deleted_at_ms IS NULL AND {clause} {suffix}",
            tuple(params),
        ).fetchall()

    def _components(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        result, seen = [], set()
        for index, raw in enumerate(values.get("components") or []):
            item = _mapping(raw)
            reagent_uuid, unit = _text(item.get("reagent_uuid")), _text(item.get("quantity_unit"))
            if not reagent_uuid or not unit:
                raise MaterialValidationError(f"components[{index}] reagent_uuid and quantity_unit are required")
            if reagent_uuid in seen:
                raise MaterialValidationError(f"duplicate component reagent_uuid {reagent_uuid}")
            seen.add(reagent_uuid)
            try:
                reagent = self.get_reagent(reagent_uuid)
            except MaterialNotFoundError:
                raise MaterialNotFoundError(f"component reagent {reagent_uuid} not found") from None
            result.append({"reagent_uuid": reagent_uuid, "reagent_info_uuid": reagent["reagent_info_uuid"],
                           "name": reagent["name"], "quantity": _quantity(item.get("quantity")), "quantity_unit": unit})
        return result

    def create_current_substance(self, values: Mapping[str, Any]) -> dict[str, Any]:
        material_uuid, unit = _text(values.get("material_uuid")), _text(values.get("quantity_unit"))
        if not material_uuid or not unit:
            raise MaterialValidationError("material_uuid and quantity_unit are required")
        quantity = _quantity(values.get("quantity"))
        with self.materials.write():
            _, substances = self._substances(material_uuid)
            if self._content_of_material(material_uuid, "current") is not None:
                raise MaterialConflictError("container already has a current substance")
            components = self._components(values)
            now = self._now()
            substance_uuid = str(uuid4())
            substances.append(MaterialSubstance(
                substance_uuid=substance_uuid, name=_text(values.get("name")) or "current substance",
                quantity=quantity, quantity_unit=unit, physical_state=_physical_state(values.get("physical_state")),
                composition=components, meta_data=_mapping(values.get("meta_data")),
            ))
            self._put_substances(material_uuid, substances, observed_at_ms=_observed_ms(values.get("observed_at"), now))
            self._insert_content(substance_uuid, material_uuid, "current", now, description=str(values.get("description") or ""))
            result = self.get_current_substance(substance_uuid)
            self._append_history(material_uuid=material_uuid, subject_type="current_substance", subject_uuid=substance_uuid,
                                 event_type="create", quantity_delta=quantity, quantity_unit=unit, revision=1,
                                 previous=None, result=result)
        return result

    def list_current_substances(self, *, page: int = 0, page_size: int = 0, material_uuid: str = "") -> dict[str, Any]:
        page, page_size = _page(page, page_size)
        clause, params = ("mc.material_uuid=?", [material_uuid.strip()]) if material_uuid.strip() else ("1=1", [])
        total = len(self._current_query(clause, params))
        rows = self._current_query(clause, params, suffix=f"ORDER BY mc.updated_at_ms DESC, mc.substance_uuid DESC LIMIT {page_size} OFFSET {(page - 1) * page_size}")
        return {"items": [self._current_row(row) for row in rows], "total": total, "page": page,
                "page_size": page_size, "has_more": page * page_size < total}

    def get_current_substance(self, identity: str) -> dict[str, Any]:
        rows = self._current_query("mc.substance_uuid=?", (identity,))
        if not rows:
            raise MaterialNotFoundError("current substance does not exist")
        return self._current_row(rows[0])

    def get_current_substance_by_material(self, material_uuid: str) -> dict[str, Any]:
        self.materials.get_material(material_uuid)
        rows = self._current_query("mc.material_uuid=?", (material_uuid,))
        if not rows:
            raise MaterialNotFoundError("current substance does not exist")
        return self._current_row(rows[0])

    def update_current_substance(self, identity: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self.materials.write():
            content = self._content(identity, "current")
            material_uuid = content["material_uuid"]
            previous = self.get_current_substance(identity)
            expected = values.get("expected_revision")
            if expected is not None and int(expected) != int(content["revision"]):
                raise MaterialConflictError("Current substance revision has changed")
            _, substances = self._substances(material_uuid)
            target = next(item for item in substances if item.substance_uuid == identity)
            quantity = _quantity(values.get("quantity", target.quantity))
            unit = _text(values.get("quantity_unit")) or target.quantity_unit
            self._assert_not_below_reserved(identity, quantity, unit)
            updated = target.model_copy(update={
                "name": _text(values.get("name")) or target.name, "quantity": quantity, "quantity_unit": unit,
                "physical_state": _physical_state(values.get("physical_state") or target.physical_state),
                "composition": self._components(values) if "components" in values else target.composition,
                "meta_data": _mapping(values.get("meta_data")) if values.get("meta_data") is not None else target.meta_data,
            })
            now = self._now()
            self._put_substances(material_uuid, [updated if item.substance_uuid == identity else item for item in substances],
                                 observed_at_ms=_observed_ms(values.get("observed_at"), now))
            self._update_content(identity, now, **({"description": str(values.get("description") or "")} if "description" in values else {}))
            result = self.get_current_substance(identity)
            self._append_history(material_uuid=material_uuid, subject_type="current_substance", subject_uuid=identity,
                                 event_type="adjust", quantity_delta=quantity - target.quantity, quantity_unit=unit,
                                 revision=result["revision"], previous=previous, result=result)
        return result

    def delete_current_substance(self, identity: str) -> None:
        self._delete_role(identity, "current", "current_substance")

    # ── 历史 ──────────────────────────────────────────────────

    @staticmethod
    def _history_row(row: Any) -> dict[str, Any]:
        return {
            "uuid": row["history_uuid"],
            "material_uuid": row["material_uuid"],
            "subject_type": row["subject_type"],
            "subject_uuid": row["subject_uuid"],
            "event_type": row["event_type"],
            "operator_type": row["operator_type"],
            "from_site_uuid": None,
            "to_site_uuid": None,
            "quantity_delta": row["quantity_delta"],
            "quantity_unit": row["quantity_unit"],
            "revision": row["revision"],
            "causation_id": row["causation_id"] or "",
            "changes": json.loads(row["changes_json"] or "{}"),
            "extension": json.loads(row["extension_json"] or "{}"),
            "trace_id": row["trace_id"],
            "recorded_at": ms_to_iso(row["recorded_at_ms"]),
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_job_uuid": row["workflow_node_job_uuid"],
        }

    def list_history(self, material_uuid: str, subject_types: Iterable[str], *, page: int = 0, page_size: int = 0) -> dict[str, Any]:
        if MaterialsRepository.get_material(self.materials, material_uuid, include_deleted=True) is None:
            raise MaterialNotFoundError("material does not exist", subject="material")
        page, page_size = _page(page, page_size)
        kinds = tuple(subject_types)
        marks = ",".join("?" for _ in kinds)
        total = int(self.connection.execute(
            f"SELECT COUNT(*) FROM material_content_history WHERE material_uuid=? AND subject_type IN ({marks})",
            (material_uuid, *kinds),
        ).fetchone()[0])
        rows = self.connection.execute(
            f"SELECT * FROM material_content_history WHERE material_uuid=? AND subject_type IN ({marks}) "
            "ORDER BY recorded_at_ms DESC, history_uuid DESC LIMIT ? OFFSET ?",
            (material_uuid, *kinds, page_size, (page - 1) * page_size),
        ).fetchall()
        return {"items": [self._history_row(row) for row in rows], "total": total, "page": page,
                "page_size": page_size, "has_more": page * page_size < total}

    def get_history(self, history_uuid: str, subject_types: Iterable[str]) -> dict[str, Any]:
        kinds = tuple(subject_types)
        row = self.connection.execute(
            f"SELECT * FROM material_content_history WHERE history_uuid=? AND subject_type IN ({','.join('?' for _ in kinds)})",
            (history_uuid, *kinds),
        ).fetchone()
        if row is None:
            raise MaterialNotFoundError("history entry does not exist")
        return self._history_row(row)

    # ── 内容物数量预留（防止并发任务超量抽取同一容器） ───────────────

    @staticmethod
    def _reservation_row(row: Any) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()} | {
            "create_time": ms_to_iso(row["created_at_ms"]), "update_time": ms_to_iso(row["updated_at_ms"]),
        }

    def reserve_content(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """按任务预留容器内某份内容物的数量；同一 reservation_uuid 重放返回原结果。"""

        substance_uuid, task_uuid, unit = (_text(values.get(key)) for key in ("substance_uuid", "task_uuid", "quantity_unit"))
        reservation_uuid = _text(values.get("reservation_uuid")) or str(uuid4())
        if not (substance_uuid and task_uuid and unit):
            raise MaterialValidationError("substance_uuid, task_uuid and quantity_unit are required")
        quantity = _quantity(values.get("quantity"))
        if quantity <= 0:
            raise MaterialValidationError("quantity must be positive")
        with self.materials.write():
            existing = self.connection.execute(
                "SELECT * FROM material_content_reservation WHERE reservation_uuid=?", (reservation_uuid,)
            ).fetchone()
            if existing is not None:
                if (existing["substance_uuid"], existing["task_uuid"], existing["quantity"], existing["quantity_unit"]) != (
                    substance_uuid, task_uuid, quantity, unit
                ):
                    raise MaterialConflictError("reservation_uuid was already used with another request")
                return self._reservation_row(existing)
            row = self.connection.execute(
                "SELECT ms.*, mc.concentration_value, mc.density_g_per_ml FROM material_substance ms "
                "JOIN material m USING(material_uuid) LEFT JOIN material_content mc USING(substance_uuid) "
                "WHERE ms.substance_uuid=? AND m.deleted_at_ms IS NULL", (substance_uuid,),
            ).fetchone()
            if row is None:
                raise MaterialNotFoundError(f"container content not found: {substance_uuid}")
            converted = convert_quantity(quantity, unit, row["quantity_unit"], inventory_type="content",
                                         physical_state=row["physical_state"],
                                         concentration_value=row["concentration_value"],
                                         density_g_per_ml=row["density_g_per_ml"])
            if converted is None:
                raise MaterialValidationError("reservation unit is incompatible with the container content")
            available = float(row["quantity"]) - self._reserved(substance_uuid, row["quantity_unit"])
            if converted[0] > available + quantity_tolerance(available, converted[0]):
                raise MaterialConflictError(
                    f"container content {substance_uuid} has {max(available, 0):g} {row['quantity_unit']} unreserved",
                    conflict="state",
                )
            now = self._now()
            self.connection.execute(
                "INSERT INTO material_content_reservation(reservation_uuid, substance_uuid, material_uuid, task_uuid, "
                "node_uuid, job_uuid, quantity, quantity_unit, status, created_at_ms, updated_at_ms, version) "
                "VALUES (?,?,?,?,?,?,?,?,'active',?,?,1)",
                (reservation_uuid, substance_uuid, row["material_uuid"], task_uuid, str(values.get("node_uuid") or ""),
                 _text(values.get("job_uuid")), quantity, unit, now, now),
            )
        return self.get_content_reservation(reservation_uuid)

    def get_content_reservation(self, reservation_uuid: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM material_content_reservation WHERE reservation_uuid=?", (reservation_uuid,)
        ).fetchone()
        if row is None:
            raise MaterialNotFoundError(f"content reservation not found: {reservation_uuid}", subject="reservation")
        return self._reservation_row(row)

    def finish_content_reservation(self, reservation_uuid: str, status: str, *, consumed_quantity: Optional[float] = None) -> dict[str, Any]:
        """``released`` 只释放；``consumed`` 同时从内容物扣减实际用量（缺省为预留量）。"""

        if status not in {"released", "consumed"}:
            raise MaterialValidationError("status must be released or consumed")
        with self.materials.write():
            row = self.connection.execute(
                "SELECT * FROM material_content_reservation WHERE reservation_uuid=?", (reservation_uuid,)
            ).fetchone()
            if row is None:
                raise MaterialNotFoundError(f"content reservation not found: {reservation_uuid}", subject="reservation")
            if row["status"] == status:
                return self._reservation_row(row)
            if row["status"] != "active":
                raise MaterialConflictError(f"content reservation is already {row['status']}", conflict="state")
            now = self._now()
            self.connection.execute(
                "UPDATE material_content_reservation SET status=?, updated_at_ms=MAX(?, created_at_ms), version=version+1 "
                "WHERE reservation_uuid=?", (status, now, reservation_uuid),
            )
            if status == "consumed":
                used = _quantity(consumed_quantity if consumed_quantity is not None else row["quantity"])
                _, substances = self._substances(row["material_uuid"])
                target = next((item for item in substances if item.substance_uuid == row["substance_uuid"]), None)
                if target is None:
                    raise MaterialNotFoundError(f"container content not found: {row['substance_uuid']}")
                converted = convert_quantity(used, row["quantity_unit"], target.quantity_unit, inventory_type="content")
                amount = converted[0] if converted else used
                if amount > target.quantity + quantity_tolerance(target.quantity, amount):
                    raise MaterialConflictError("consumption exceeds the container content", conflict="state")
                balance_after = max(0.0, target.quantity - amount)
                updated = target.model_copy(update={"quantity": balance_after})
                self._put_substances(row["material_uuid"],
                                     [updated if item.substance_uuid == target.substance_uuid else item for item in substances],
                                     job_uuid=row["job_uuid"])
                kind = self.connection.execute(
                    "SELECT content_kind FROM material_content WHERE substance_uuid=?", (target.substance_uuid,)
                ).fetchone()
                if kind is not None:
                    self._update_content(target.substance_uuid, now)
                # 消费事实（计划量、上报量、按内容物单位的实际量与前后余量）写进历史扩展，作业 / 试剂消费
                # 读模型据此还原；没有角色的内容物按 current_substance 记
                self._append_history(
                    material_uuid=row["material_uuid"],
                    subject_type={"current": "current_substance"}.get(kind[0], kind[0]) if kind else "current_substance",
                    subject_uuid=target.substance_uuid, event_type="consume", quantity_delta=-amount,
                    quantity_unit=target.quantity_unit, revision=None, previous=None, result=None,
                    extension={
                        "reservation_uuid": reservation_uuid,
                        "planned_quantity": float(row["quantity"]),
                        "planned_quantity_unit": row["quantity_unit"],
                        "reported_quantity": used,
                        "reported_quantity_unit": row["quantity_unit"],
                        "actual_quantity": amount,
                        "quantity_unit": target.quantity_unit,
                        "balance_before": target.quantity,
                        "balance_after": balance_after,
                    },
                    operator_type="workflow", task_uuid=row["task_uuid"], job_uuid=row["job_uuid"],
                )
        return self.get_content_reservation(reservation_uuid)

    def list_content_reservations(self, *, status: str = "", task_uuid: str = "", limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if task_uuid:
            clauses.append("task_uuid=?")
            params.append(task_uuid)
        rows = self.connection.execute(
            f"SELECT * FROM material_content_reservation WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at_ms DESC, reservation_uuid LIMIT ?", (*params, limit),
        ).fetchall()
        return [self._reservation_row(row) for row in rows]

    # ── 物料来源绑定（task_exclusive / shared_source） ──────────────

    @staticmethod
    def _binding_row(row: Any) -> dict[str, Any]:
        result = {key: row[key] for key in row.keys() if key != "selector_json"}
        result["selector"] = json.loads(row["selector_json"] or "{}")
        return result

    def bind_material_source(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """任务准入时冻结来源物料：独占来源与任何其他任务互斥；共享来源只与独占互斥。

        同一 (task, node, attempt) 重放必须给出相同的物料与选择器，否则冲突。
        """

        task_uuid, node_uuid, material_uuid = (_text(values.get(key)) for key in ("task_uuid", "node_uuid", "material_uuid"))
        policy = str(values.get("custody_policy") or "task_exclusive")
        if not (task_uuid and node_uuid and material_uuid):
            raise MaterialValidationError("task_uuid, node_uuid and material_uuid are required")
        if policy not in {"task_exclusive", "shared_source"}:
            raise MaterialValidationError("custody_policy must be task_exclusive or shared_source")
        attempt = int(values.get("attempt") or 1)
        selector = _mapping(values.get("selector"))
        with self.materials.write():
            aggregate = self.materials.get_material(material_uuid)
            existing = self.connection.execute(
                "SELECT * FROM material_source_binding WHERE task_uuid=? AND node_uuid=? AND attempt=?",
                (task_uuid, node_uuid, attempt),
            ).fetchone()
            if existing is not None:
                if (existing["material_uuid"], existing["custody_policy"], json.loads(existing["selector_json"])) != (
                    material_uuid, policy, selector,
                ):
                    raise MaterialConflictError("material source binding replay changed its selector")
                return self._binding_row(existing)
            others = self.connection.execute(
                "SELECT custody_policy, task_uuid FROM material_source_binding "
                "WHERE material_uuid=? AND status='active' AND task_uuid<>?", (material_uuid, task_uuid),
            ).fetchall()
            if any(policy == "task_exclusive" or row["custody_policy"] == "task_exclusive" for row in others):
                raise MaterialConflictError(
                    f"material {material_uuid} is bound by another task", conflict="active_claim"
                )
            binding_uuid = str(uuid4())
            self.connection.execute(
                "INSERT INTO material_source_binding(binding_uuid, task_uuid, node_uuid, attempt, material_uuid, "
                "template_uuid, custody_policy, selector_json, status, created_at_ms, version) "
                "VALUES (?,?,?,?,?,?,?,?,'active',?,1)",
                (binding_uuid, task_uuid, node_uuid, attempt, material_uuid, aggregate.material.template_uuid, policy,
                 json.dumps(selector, ensure_ascii=False, sort_keys=True), self._now()),
            )
            row = self.connection.execute(
                "SELECT * FROM material_source_binding WHERE binding_uuid=?", (binding_uuid,)
            ).fetchone()
        return self._binding_row(row)

    def release_material_sources(self, task_uuid: str, *, node_uuid: Optional[str] = None) -> list[str]:
        with self.materials.write():
            clauses, params = ["task_uuid=?", "status='active'"], [task_uuid]
            if node_uuid:
                clauses.append("node_uuid=?")
                params.append(node_uuid)
            rows = self.connection.execute(
                f"SELECT binding_uuid FROM material_source_binding WHERE {' AND '.join(clauses)}", params
            ).fetchall()
            now = self._now()
            for row in rows:
                self.connection.execute(
                    "UPDATE material_source_binding SET status='released', released_at_ms=MAX(?, created_at_ms), "
                    "version=version+1 WHERE binding_uuid=?", (now, row[0]),
                )
        return [row[0] for row in rows]

    def list_material_source_bindings(self, *, material_uuid: str = "", task_uuid: str = "", status: str = "") -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        for column, value in (("material_uuid", material_uuid), ("task_uuid", task_uuid), ("status", status)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        rows = self.connection.execute(
            f"SELECT * FROM material_source_binding WHERE {' AND '.join(clauses)} ORDER BY created_at_ms, binding_uuid",
            params,
        ).fetchall()
        return [self._binding_row(row) for row in rows]


def batch_create(rows: Sequence[Mapping[str, Any]], *, atomic: bool, batch: Callable[[Sequence[Mapping[str, Any]]], list],
                 single: Callable[[Mapping[str, Any]], Any]) -> dict[str, Any]:
    """V1 批量导入：atomic 时整批成败；否则逐行提交并回报失败行号（从 1 起）。"""

    if not rows:
        raise MaterialValidationError("items is required")
    if len(rows) > _MAX_BATCH:
        raise MaterialValidationError(f"at most {_MAX_BATCH} rows can be imported at once")
    if atomic:
        created = batch(rows)
        return {"total": len(rows), "created": len(created), "failed": 0, "items": created, "errors": []}
    created, errors = [], []
    for index, row in enumerate(rows, start=1):
        try:
            created.append(single(row))
        except (MaterialValidationError, MaterialConflictError, MaterialNotFoundError, ChemicalError) as error:
            errors.append({"row": index, "msg": getattr(error, "message", None) or str(error)})
    return {"total": len(rows), "created": len(created), "failed": len(errors), "items": created, "errors": errors}


def request_hash(command: Mapping[str, Any]) -> str:
    return canonical_hash(dict(command))


__all__ = ["ContentContract", "batch_create", "request_hash"]
