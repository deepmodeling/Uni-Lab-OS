"""按冻结输出合同生成中心公开值；本地物料事实和库存证据保持独立。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from unilabos.workflow.schema import parse_output_contract


def _project_value(schema: dict[str, Any], value: Any) -> Any:
    if "anyOf" in schema:
        return None if value is None else _project_value(schema["anyOf"][0], value)
    if schema.get("$slot") == "ResourceSlot":
        identity = value.get("uuid") if isinstance(value, dict) else None
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError("ResourceSlot 公开输出缺少规范 UUID")
        # custody_policy、resource_template_uuid 等是本地运行事实；中心公开
        # ResourceSlot 始终是闭合 {uuid}，物料谱系另由权威 evidence 表达。
        return {"uuid": identity}
    if schema.get("type") == "array":
        if not isinstance(value, list):
            raise ValueError("公开数组输出与冻结合同不一致")
        return [_project_value(schema["items"], item) for item in value]
    return deepcopy(value)


def project_public_workflow_output(output_contract: Any, output: dict[str, Any]) -> dict[str, Any]:
    """仅投影声明为 ResourceSlot 的值，不改原结果或裁剪普通对象字段。

    参数为任务冻结输出合同和真实 workflow_result。返回独立公开副本；合同
    非法或 ResourceSlot 身份无法证明时抛 ValueError，由调用边界保留待核实。
    """
    descriptors = parse_output_contract(output_contract).to_dict()["outputs"]
    projected = deepcopy(output)
    for descriptor in descriptors:
        name = descriptor["name"]
        if name in output:
            projected[name] = _project_value(descriptor["schema"], output[name])
    return projected
