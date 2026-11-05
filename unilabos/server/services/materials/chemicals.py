"""化学品定义（chemical）：物料内容物引用的化学身份，落 materials.db。

一条化学品定义以 ``chemical_uuid`` 为主键；CAS 登记号与 InChIKey 是可空的唯一键
（只在未删除的行之间唯一，软删除后可以重新登记）。CAS 校验格式与校验位，
InChIKey 校验 27 位标准格式并统一大写。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from unilabos.server.services.materials.store import MaterialsRepository

_CAS = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")
_INCHI_KEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
PHYSICAL_STATES = frozenset({"liquid", "solid", "gas", "unknown"})
_FIELDS = (
    "name", "name_en", "aliases", "cas", "inchi_key", "molecular_formula", "smiles",
    "molecular_weight", "density_g_per_ml", "physical_state", "description", "meta_data",
)


class ChemicalError(Exception):
    """化学品定义的业务错误；``code`` 映射到 HTTP envelope 业务码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def normalize_cas(value: Any) -> str:
    """校验并规范化 CAS 登记号（格式 + 校验位）。"""

    cas = str(value or "").strip()
    match = _CAS.fullmatch(cas)
    if match is None:
        raise ChemicalError("invalid_input", f"CAS 登记号格式不正确：{cas!r}")
    digits = "".join(match.groups()[:2])
    checksum = sum(int(char) * weight for weight, char in enumerate(reversed(digits), 1)) % 10
    if checksum != int(match.group(3)):
        raise ChemicalError("invalid_input", f"CAS 登记号校验位不正确：{cas!r}")
    return cas


def normalize_inchi_key(value: Any) -> str:
    key = str(value or "").strip().upper()
    if not _INCHI_KEY.fullmatch(key):
        raise ChemicalError("invalid_input", f"InChIKey 格式不正确：{value!r}")
    return key


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive(value: Any, field: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ChemicalError("invalid_input", f"{field} 必须是正数") from None
    if not number > 0:
        raise ChemicalError("invalid_input", f"{field} 必须是正数")
    return number


def _now_ms() -> int:
    return int(time.time() * 1000)


class ChemicalService(MaterialsRepository):
    """化学品定义的增删改查；与物料共用 materials.db 的连接和写锁。"""

    def __init__(self, database: "str | Path | MaterialsRepository") -> None:
        super().__init__(database)

    # ── 校验 ────────────────────────────────────────────────

    @staticmethod
    def _normalize(values: Mapping[str, Any], *, partial: bool) -> dict[str, Any]:
        unknown = set(values) - set(_FIELDS)
        if unknown:
            raise ChemicalError("invalid_input", f"未知字段：{', '.join(sorted(unknown))}")
        result: dict[str, Any] = {}
        if "name" in values or not partial:
            name = _optional_text(values.get("name"))
            if not name:
                raise ChemicalError("invalid_input", "name 不能为空")
            result["name"] = name
        for field in ("name_en", "molecular_formula", "smiles"):
            if field in values:
                result[field] = _optional_text(values[field])
        if "description" in values:
            result["description"] = str(values["description"] or "")
        if "aliases" in values:
            aliases = values["aliases"] or []
            if isinstance(aliases, (str, bytes)) or not isinstance(aliases, (list, tuple)):
                raise ChemicalError("invalid_input", "aliases 必须是字符串数组")
            cleaned: list[str] = []
            for alias in aliases:
                text = _optional_text(alias)
                if text and text not in cleaned:
                    cleaned.append(text)
            result["aliases"] = cleaned
        if "cas" in values:
            result["cas"] = normalize_cas(values["cas"]) if _optional_text(values["cas"]) else None
        if "inchi_key" in values:
            result["inchi_key"] = (
                normalize_inchi_key(values["inchi_key"]) if _optional_text(values["inchi_key"]) else None
            )
        for field in ("molecular_weight", "density_g_per_ml"):
            if field in values:
                result[field] = _positive(values[field], field)
        if "physical_state" in values or not partial:
            state = str(values.get("physical_state") or "unknown").strip().lower()
            if state not in PHYSICAL_STATES:
                raise ChemicalError("invalid_input", f"physical_state 只能是 {sorted(PHYSICAL_STATES)}")
            result["physical_state"] = state
        if "meta_data" in values:
            meta = values["meta_data"] or {}
            if not isinstance(meta, Mapping):
                raise ChemicalError("invalid_input", "meta_data 必须是对象")
            result["meta_data"] = dict(meta)
        return result

    @staticmethod
    def _read(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "chemical_uuid": row["chemical_uuid"],
            "name": row["name"],
            "name_en": row["name_en"],
            "aliases": json.loads(row["aliases_json"]),
            "cas": row["cas"],
            "inchi_key": row["inchi_key"],
            "molecular_formula": row["molecular_formula"],
            "smiles": row["smiles"],
            "molecular_weight": row["molecular_weight"],
            "density_g_per_ml": row["density_g_per_ml"],
            "physical_state": row["physical_state"],
            "description": row["description"],
            "meta_data": json.loads(row["meta_data_json"]),
            "created_at_ms": row["created_at_ms"],
            "updated_at_ms": row["updated_at_ms"],
            "version": row["version"],
        }

    @staticmethod
    def _duplicate(error: sqlite3.IntegrityError) -> ChemicalError:
        message = str(error)
        if "cas" in message:
            return ChemicalError("conflict", "已有相同 CAS 登记号的化学品")
        if "inchi_key" in message:
            return ChemicalError("conflict", "已有相同 InChIKey 的化学品")
        return ChemicalError("invalid_input", f"化学品数据不合法：{message}")

    def _row(self, chemical_uuid: str) -> sqlite3.Row:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM chemical WHERE chemical_uuid = ? AND deleted_at_ms IS NULL",
                (str(chemical_uuid),),
            ).fetchone()
        if row is None:
            raise ChemicalError("not_found", f"化学品 {chemical_uuid} 不存在")
        return row

    # ── 读 ──────────────────────────────────────────────────

    def get_chemical(self, chemical_uuid: str) -> dict[str, Any]:
        return self._read(self._row(chemical_uuid))

    def list_chemicals(
        self,
        *,
        q: str = "",
        cas: str = "",
        inchi_key: str = "",
        physical_state: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 500)
        where = ["deleted_at_ms IS NULL"]
        params: list[Any] = []
        if cas.strip():
            where.append("cas = ?")
            params.append(cas.strip())
        if inchi_key.strip():
            where.append("inchi_key = ?")
            params.append(inchi_key.strip().upper())
        if physical_state.strip():
            where.append("physical_state = ?")
            params.append(physical_state.strip().lower())
        if q.strip():
            pattern = f"%{q.strip().lower()}%"
            where.append(
                "(LOWER(name) LIKE ? OR LOWER(COALESCE(name_en, '')) LIKE ? OR LOWER(aliases_json) LIKE ? "
                "OR COALESCE(cas, '') LIKE ? OR LOWER(COALESCE(molecular_formula, '')) LIKE ?)"
            )
            params.extend([pattern] * 5)
        clause = " AND ".join(where)
        with self._lock:
            total = self.connection.execute(
                f"SELECT COUNT(*) FROM chemical WHERE {clause}", params
            ).fetchone()[0]
            rows = self.connection.execute(
                f"SELECT * FROM chemical WHERE {clause} ORDER BY LOWER(name), chemical_uuid LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {"items": [self._read(row) for row in rows], "total": int(total), "page": page, "page_size": page_size}

    # ── 写 ──────────────────────────────────────────────────

    def create_chemical(self, values: Mapping[str, Any], *, chemical_uuid: Optional[str] = None) -> dict[str, Any]:
        data = self._normalize(values, partial=False)
        identity = str(chemical_uuid or uuid4())
        now = _now_ms()
        try:
            with self.write() as conn:
                conn.execute(
                    """
                    INSERT INTO chemical(
                        chemical_uuid, name, name_en, aliases_json, cas, inchi_key, molecular_formula,
                        smiles, molecular_weight, density_g_per_ml, physical_state, description,
                        meta_data_json, created_at_ms, updated_at_ms, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        identity, data["name"], data.get("name_en"),
                        json.dumps(data.get("aliases", []), ensure_ascii=False),
                        data.get("cas"), data.get("inchi_key"), data.get("molecular_formula"),
                        data.get("smiles"), data.get("molecular_weight"), data.get("density_g_per_ml"),
                        data["physical_state"], data.get("description", ""),
                        json.dumps(data.get("meta_data", {}), ensure_ascii=False), now, now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "chemical.chemical_uuid" in str(error):
                raise ChemicalError("conflict", f"化学品 {identity} 已存在") from None
            raise self._duplicate(error) from None
        return self.get_chemical(identity)

    def update_chemical(
        self, chemical_uuid: str, values: Mapping[str, Any], *, expected_version: int
    ) -> dict[str, Any]:
        data = self._normalize(values, partial=True)
        current = self._read(self._row(chemical_uuid))
        if current["version"] != expected_version:
            raise ChemicalError("conflict", "化学品已被修改，请刷新后重试")
        merged = {**current, **data}
        now = max(_now_ms(), int(current["updated_at_ms"]))
        try:
            with self.write() as conn:
                changed = conn.execute(
                    """
                    UPDATE chemical SET name=?, name_en=?, aliases_json=?, cas=?, inchi_key=?,
                        molecular_formula=?, smiles=?, molecular_weight=?, density_g_per_ml=?,
                        physical_state=?, description=?, meta_data_json=?, updated_at_ms=?,
                        version=version + 1
                    WHERE chemical_uuid=? AND version=? AND deleted_at_ms IS NULL
                    """,
                    (
                        merged["name"], merged.get("name_en"),
                        json.dumps(merged.get("aliases", []), ensure_ascii=False),
                        merged.get("cas"), merged.get("inchi_key"), merged.get("molecular_formula"),
                        merged.get("smiles"), merged.get("molecular_weight"), merged.get("density_g_per_ml"),
                        merged["physical_state"], merged.get("description", ""),
                        json.dumps(merged.get("meta_data", {}), ensure_ascii=False), now,
                        str(chemical_uuid), expected_version,
                    ),
                ).rowcount
        except sqlite3.IntegrityError as error:
            raise self._duplicate(error) from None
        if not changed:
            raise ChemicalError("conflict", "化学品已被修改，请刷新后重试")
        return self.get_chemical(chemical_uuid)

    def delete_chemical(self, chemical_uuid: str, *, expected_version: int) -> None:
        """软删除：CAS / InChIKey 让出，之后可以重新登记同一化学品。"""

        current = self._read(self._row(chemical_uuid))
        if current["version"] != expected_version:
            raise ChemicalError("conflict", "化学品已被修改，请刷新后重试")
        now = max(_now_ms(), int(current["updated_at_ms"]))
        with self.write() as conn:
            changed = conn.execute(
                "UPDATE chemical SET deleted_at_ms=?, updated_at_ms=?, version=version + 1 "
                "WHERE chemical_uuid=? AND version=? AND deleted_at_ms IS NULL",
                (now, now, str(chemical_uuid), expected_version),
            ).rowcount
        if not changed:
            raise ChemicalError("conflict", "化学品已被修改，请刷新后重试")


__all__ = ["ChemicalError", "ChemicalService", "PHYSICAL_STATES", "normalize_cas", "normalize_inchi_key"]
