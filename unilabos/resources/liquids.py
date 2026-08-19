"""历史兼容入口；新代码请使用 :mod:`unilabos.resources.materials`。"""

from unilabos.resources.materials import (
    LIQUID_UNIT,
    SELF_SLOT,
    SOLID_UNIT,
    apply_substances,
    resolve_site_spot,
    resolve_substance_targets,
    set_substance_on_target,
)

__all__ = [
    "LIQUID_UNIT",
    "SELF_SLOT",
    "SOLID_UNIT",
    "apply_substances",
    "resolve_site_spot",
    "resolve_substance_targets",
    "set_substance_on_target",
]
