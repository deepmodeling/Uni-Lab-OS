"""任务准入时读的库位事实（调度权威的工作流服务消费，只读不写、不做判定）。

物料参数按库位绑定（``{"site_uuid": ...}``）在建任务 / 运行预检时解析：这里读库位此刻的占用物料及其模板
（模板名、``module_name``、类别），由工作流服务换成 ``{"uuid": 占用物料}`` 并冻结进任务。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional


def _template_facts(service: Any, template_uuid: str) -> dict[str, Any]:
    try:
        template = service.get_template(template_uuid)
    except Exception:  # noqa: BLE001 - 模板读不到时只给身份，由调用方按「模板不符」处理
        return {"template_uuid": template_uuid, "module_name": None, "category": []}
    return {
        "template_uuid": template.template_uuid,
        "module_name": template.module_name,
        "category": list(template.category or []),
    }


def _material_facts(service: Any, material_uuid: str) -> Optional[dict[str, Any]]:
    from .store import MaterialsRepository

    record = MaterialsRepository.get_material(service, material_uuid)
    if record is None or record.deleted_at_ms is not None:
        return None
    return {
        "material_uuid": record.material_uuid,
        "display_name": record.display_name,
        "template_name": record.template_name,
        "lifecycle_status": record.lifecycle_status,
        **_template_facts(service, record.template_uuid),
    }


def site_admission_facts(service: Any, *, site_uuids: Iterable[str] = ()) -> dict[str, Any]:
    """一次读出按库位绑定要的事实：``{"sites": {site_uuid: {...}}}``。

    不存在（或已删除）的库位 ``exists=False``；存在的给 ``label``、所属物料、占用物料 ``occupant``
    （``material_uuid`` / ``template_uuid`` / ``template_name`` / ``module_name`` / ``category`` /
    ``lifecycle_status``；空库位为 ``None``）。
    """

    from .store import MaterialsRepository

    identities = sorted({str(item).strip() for item in site_uuids if str(item or "").strip()})
    with service._write_lock:
        conn = service.connection
        # 同一个读事务：库位占用与占用物料是同一时刻的事实
        nested = conn.in_transaction
        if not nested:
            conn.execute("BEGIN")
        try:
            sites: dict[str, Any] = {}
            for identity in identities:
                site = MaterialsRepository.get_site(service, identity)
                if site is None:
                    sites[identity] = {"site_uuid": identity, "exists": False}
                    continue
                occupant = (
                    _material_facts(service, site.occupied_material_uuid) if site.occupied_material_uuid else None
                )
                sites[identity] = {
                    "site_uuid": identity,
                    "exists": True,
                    "label": site.label,
                    "owner_material_uuid": site.owner_material_uuid,
                    "occupied_material_uuid": site.occupied_material_uuid,
                    "occupant": occupant,
                }
            return {"sites": sites}
        finally:
            if not nested:
                conn.rollback()


__all__ = ["site_admission_facts"]
