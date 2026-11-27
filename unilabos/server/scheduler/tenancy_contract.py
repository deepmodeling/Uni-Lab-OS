"""从冻结的动作合同和实际参数解析托管与按类型的设备 / 库位锁；设备使用物料权威的可读 resource_id。

- 设备托管（``MaterialRole.load`` / ``unload``，实验性）：设备参数是 ``DeviceSlot``，省略时（合同里的
  ``EXECUTING_DEVICE`` 记号）是执行设备；
- 按类型加锁：``DeviceSlot`` / ``list[DeviceSlot]`` 参数加设备锁（``device_claims``），``SiteSlot`` 参数加库位锁
  （``site_claims``，按「所属物料 + 库位」）；字段由注册表标出 ``x-unilabos-slot``。空值不加锁；库位参数的值是
  ``MaterialRole.site_picker`` 所属方下的库位名时按解析出的库位加锁，解析不出时不加库位锁。
"""
from unilabos.registry.action_resource_contract import EXECUTING_DEVICE
from unilabos.server.scheduler.models import DeviceTenancyTransition, DeviceTenancyRequirement, SiteLockClaim


def slot_parameters(schema: dict | None, kind: str) -> list[str]:
    """schema 里注解为 ``DeviceSlot``（``kind="device"``，含列表）或 ``SiteSlot``（``kind="site"``）的参数名。"""

    from unilabos.registry.action_schema import slot_kind

    fields = (((schema or {}).get("properties") or {}).get("goal") or {}).get("properties") or {}
    return [name for name, field in fields.items() if slot_kind(field) in {kind, f"{kind}_list"}]


def _slot_values(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def resolve_site_claims(schema: dict | None, values: dict, materials, device_id: str | None = None) -> list[SiteLockClaim]:
    """``SiteSlot`` 参数 → 库位锁（所属物料 + 库位）。

    值是库位 uuid 时所属方由库位决定；是 ``MaterialRole.site_picker`` 所属方下的库位名时按解析出的库位加锁；
    两者都解析不出时不加锁（派发前的物料 / 物理合同会报错）。
    """

    names = slot_parameters(schema, "site")
    if not names or materials is None:
        return []
    from .physical_contract import site_by_label, site_owner_material_uuid

    claims: dict[tuple[str, str], SiteLockClaim] = {}
    for name in names:
        for site_uuid in _slot_values(values.get(name)):
            owner = site_owner_material_uuid(materials, site_uuid)
            if owner is None:
                try:
                    found = site_by_label(schema, values, name, materials, device_id)
                except (ValueError, RuntimeError, OSError):
                    found = None
                if found is None:
                    continue
                owner, site_uuid = found
            claims[(owner, site_uuid)] = SiteLockClaim(owner_material_uuid=owner, site_uuid=site_uuid)
    return [claims[key] for key in sorted(claims)]


def resolve_tenancy_contract(schema: dict | None, values: dict, device_id: str, materials) -> dict:
    contract = ((schema or {}).get("x-unilabos-action-contract") or {}).get("resource_contract") or {}
    transition = contract.get("device_tenancy")
    in_place = contract.get("operate_in_place")
    devices = set(contract.get("required_device_params", []))
    devices.update(item["param"] for item in contract.get("resource_params", []) if item["role"] in {"device","motion"})
    if transition:
        devices.update(transition[name] for name in ("acquire_device_param","release_device_param") if transition.get(name))
    # 按类型加锁：DeviceSlot 参数（含列表）一律加设备锁，空值不加
    typed_devices = [name for name in slot_parameters(schema, "device") if _slot_values(values.get(name))]
    site_claims = resolve_site_claims(schema, values, materials, device_id)
    if not (transition or in_place or devices or typed_devices or site_claims):
        return {}

    def identity(name):
        value = values.get(name)
        if not isinstance(value, dict) or not isinstance(value.get("uuid"), str) or not value["uuid"].strip():
            raise ValueError(f"资源参数 {name} 缺少物料身份")
        return value["uuid"]

    def registered(name, value):
        if not value.strip() or value != value.strip():
            raise ValueError(f"设备参数 {name} 必须给出非空 ID")
        aggregate = materials.get_material_by_resource_id(value)
        if aggregate.material.resource_id != value:
            raise ValueError(f"设备参数 {name} 的身份映射不一致")
        return aggregate

    def device(name):
        if name == EXECUTING_DEVICE:
            return device_id
        if materials is None:
            raise ValueError("设备托管需要物料权威解析设备身份")
        value = values.get(name)
        if isinstance(value, str):
            aggregate = registered(name, value)
        else:
            aggregate = materials.get_material(identity(name))
        if aggregate.material.resource_type != "device" or not aggregate.material.resource_id:
            raise ValueError(f"托管参数 {name} 必须指向已登记设备")
        return aggregate.material.resource_id

    ids = {name: device(name) for name in devices}
    claimed = set(ids.values())
    for name in typed_devices:
        if materials is None:
            raise ValueError("设备参数需要物料权威解析设备身份")
        for value in _slot_values(values.get(name)):
            aggregate = registered(name, value)
            if aggregate.material.resource_type != "device":
                raise ValueError(f"设备参数 {name} 必须指向已登记设备")
            claimed.add(value)
    result = {"device_claims": sorted(claimed)}
    if site_claims:
        result["site_claims"] = site_claims
    if transition:
        result["device_tenancy"] = DeviceTenancyTransition(material_uuid=identity(transition["material_param"]),
            acquire_device_id=ids.get(transition.get("acquire_device_param")),
            release_device_id=ids.get(transition.get("release_device_param")))
    if in_place:
        result["operate_in_place"] = DeviceTenancyRequirement(material_uuid=identity(in_place["material_param"]), device_id=device_id)
    return result
