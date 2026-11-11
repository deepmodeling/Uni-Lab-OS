"""从冻结的动作合同和实际参数解析托管；设备使用物料权威的可读 resource_id。"""
from unilabos.server.backend.scheduler.models import DeviceTenancyTransition, DeviceTenancyRequirement


def resolve_tenancy_contract(schema: dict | None, values: dict, device_id: str, materials) -> dict:
    contract = ((schema or {}).get("x-unilabos-action-contract") or {}).get("resource_contract") or {}
    transition = contract.get("device_tenancy")
    in_place = contract.get("operate_in_place")
    devices = set(contract.get("required_device_params", []))
    devices.update(item["param"] for item in contract.get("resource_params", []) if item["role"] in {"device","motion"})
    if transition:
        devices.update(transition[name] for name in ("acquire_device_param","release_device_param") if transition.get(name))
    if not (transition or in_place or devices):
        return {}

    def identity(name):
        value = values.get(name)
        if not isinstance(value, dict) or not isinstance(value.get("uuid"), str) or not value["uuid"].strip():
            raise ValueError(f"资源参数 {name} 缺少物料身份")
        return value["uuid"]

    def device(name):
        if materials is None:
            raise ValueError("设备托管需要物料权威解析设备身份")
        value = values.get(name)
        if isinstance(value, str):
            if not value.strip() or value != value.strip():
                raise ValueError(f"设备参数 {name} 必须给出非空 ID")
            aggregate = materials.get_material_by_resource_id(value)
            if aggregate.material.resource_id != value:
                raise ValueError(f"设备参数 {name} 的身份映射不一致")
        else:
            aggregate = materials.get_material(identity(name))
        if aggregate.material.resource_type != "device" or not aggregate.material.resource_id:
            raise ValueError(f"托管参数 {name} 必须指向已登记设备")
        return aggregate.material.resource_id

    ids = {name: device(name) for name in devices}
    result = {"device_claims": sorted(set(ids.values()))}
    if transition:
        result["device_tenancy"] = DeviceTenancyTransition(material_uuid=identity(transition["material_param"]),
            acquire_device_id=ids.get(transition.get("acquire_device_param")),
            release_device_id=ids.get(transition.get("release_device_param")))
    if in_place:
        result["operate_in_place"] = DeviceTenancyRequirement(material_uuid=identity(in_place["material_param"]), device_id=device_id)
    return result
