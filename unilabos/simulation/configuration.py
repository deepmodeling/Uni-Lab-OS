"""仿真配置迁移检查，防止旧配置被忽略后使用默认执行来源。"""

import os
from collections.abc import Mapping


def validate_simulation_environment(environ: Mapping[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    replacements = {
        "UNILABOS_TWIN_ENABLED": "UNILABOS_OBSERVATIONS_ENABLED",
        "UNILABOS_TWIN_PROVIDER_CONFIG": "UNILABOS_SIMULATION_PROVIDER_CONFIG",
        "UNILABOS_TWIN_ACTION_SOCKET": "UNILABOS_SIMULATION_ACTION_SOCKET",
    }
    for old, new in replacements.items():
        if old in values:
            raise ValueError(f"配置 {old} 已移除，请迁移为 {new}")
