"""任务准入时读的库位事实（调度权威的工作流服务 / 调度器消费，只读不写、不做判定）。

两种按库位的写法在准入时解析成普通参数：

- 物料参数按库位绑定（``{"site_uuid": ...}``）：读库位此刻的占用物料及其模板（模板名、``module_name``、类别），
  由工作流服务换成 ``{"uuid": 占用物料}`` 并冻结进任务；
- 库位参数按所属物料选择（``{"owner_uuid": ...}``）：读所属物料下接受放入物料的库位（库位的接收规则：
  模板白名单 ``extra.allowed_resource_template_uuids`` 与 ``allowed_resource_categories``），按库位序号排好，
  并标出此刻能用的（空着、没有活动入口预留、没有未结算物理动作占着）。真正选定与预留走入口预留
  （``ingress.py`` 的 ``owner_material_uuid`` 写法），这里只给建任务与运行预检看。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from unilabos.server.database.tables.materials import SiteRecord


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


def site_accepts_material(site: SiteRecord, material: Mapping[str, Any]) -> bool:
    """库位的接收规则是否接受这件物料：模板白名单（空表示不限）与允许的资源类别（空表示不限）都要满足。"""

    from .core import MaterialsService

    if not MaterialsService.site_accepts_template(
        site, template_uuid=str(material.get("template_uuid") or ""),
        template_name=str(material.get("template_name") or ""),
    ):
        return False
    allowed = {str(item).casefold() for item in site.allowed_resource_categories or []}
    if not allowed:
        return True
    return bool(allowed & {str(item).casefold() for item in material.get("category") or []})


def acceptable_owner_sites(service: Any, owner_material_uuid: str, occupant: Mapping[str, Any]) -> list[SiteRecord]:
    """所属物料下（可见的）接受这件物料的库位，按库位序号排序。"""

    from .store import MaterialsRepository

    return [
        site for site in MaterialsRepository.list_sites(service, owner_material_uuid)
        if site.visible and site_accepts_material(site, occupant)
    ]


def occupant_facts(service: Any, material_uuid: str) -> Optional[dict[str, Any]]:
    """放入物料的模板事实（接收规则判定用）；物料不存在或已删除时为 ``None``。"""

    return _material_facts(service, material_uuid)


def _available(conn: Any, site_uuids: list[str], now_ms: int) -> list[str]:
    """空着、没有活动入口预留（未到期的 reserved 或 in_transit）、没有未结算物理动作占着的库位。"""

    if not site_uuids:
        return []
    marks = ",".join("?" for _ in site_uuids)
    rows = conn.execute(
        "SELECT site.site_uuid FROM site LEFT JOIN station_ingress_reservation AS ingress "
        "ON ingress.site_uuid = site.site_uuid AND (ingress.state='in_transit' OR "
        "(ingress.state='reserved' AND ingress.expires_at_ms > ?)) "
        "LEFT JOIN material_execution_hold AS hold "
        "ON hold.aggregate_type='site' AND hold.aggregate_uuid = site.site_uuid "
        f"WHERE site.site_uuid IN ({marks}) AND site.deleted_at_ms IS NULL "
        "AND site.occupied_material_uuid IS NULL AND ingress.reservation_uuid IS NULL AND hold.job_uuid IS NULL",
        (now_ms, *site_uuids),
    ).fetchall()
    found = {row[0] for row in rows}
    return [identity for identity in site_uuids if identity in found]


def _owner_facts(service: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    from .store import MaterialsRepository

    owner_uuid = str(request.get("owner_material_uuid") or "").strip()
    occupant_uuid = str(request.get("occupant_material_uuid") or "").strip()
    owner = MaterialsRepository.get_material(service, owner_uuid) if owner_uuid else None
    owner_exists = owner is not None and owner.deleted_at_ms is None
    occupant = _material_facts(service, occupant_uuid) if occupant_uuid else None
    acceptable = acceptable_owner_sites(service, owner_uuid, occupant) if owner_exists and occupant else []
    return {
        "owner_material_uuid": owner_uuid,
        "occupant_material_uuid": occupant_uuid,
        "owner_exists": owner_exists,
        "occupant": occupant,
        "acceptable": [{"site_uuid": site.site_uuid, "label": site.label} for site in acceptable],
        "available": _available(service.connection, [site.site_uuid for site in acceptable], service._now_ms()),
    }


def site_admission_facts(
    service: Any, *, site_uuids: Iterable[str] = (), owners: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """一次读出按库位绑定 / 按所属物料选择要的事实：``{"sites": {site_uuid: {...}}, "owners": [...]}``。

    - ``sites``：不存在（或已删除）的库位 ``exists=False``；存在的给 ``label``、所属物料、占用物料 ``occupant``
      （``material_uuid`` / ``template_uuid`` / ``template_name`` / ``module_name`` / ``category`` /
      ``lifecycle_status``；空库位为 ``None``）；
    - ``owners``：按请求（``{"owner_material_uuid", "occupant_material_uuid"}``）顺序，每项给 ``owner_exists``、
      放入物料 ``occupant``（同上；不存在为 ``None``）、接受它的库位 ``acceptable``（``[{"site_uuid", "label"}]``，
      按库位序号）与此刻能用的 ``available``（库位 uuid 列表）。
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
            return {"sites": sites, "owners": [_owner_facts(service, request) for request in owners]}
        finally:
            if not nested:
                conn.rollback()


__all__ = ["acceptable_owner_sites", "occupant_facts", "site_accepts_material", "site_admission_facts"]
