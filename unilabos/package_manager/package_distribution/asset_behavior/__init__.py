"""资产行为构建（Asset Behavior Build）的公开 Interface。

构建期把固定工程包里 ``behavior_binding.kind=asset_python`` 的源码原样固化到领域包
``_asset_code``，并生成领域包自有的静态 ``@device`` 转发声明。只用标准库，不导入
包作者代码、资产代码或 OS 其他层。
"""

from .asset_rails import (
    carrier_at,
    carrier_pose,
    check_layout,
    covered,
    grid_at,
    mount_pose,
    rail_mount_from_asset,
    rails_from_asset,
)
from .asset_sites import FRAME as SITE_FRAME
from .asset_sites import apply_site_map
from .forwarder import (
    ForwarderOptions,
    generate_forwarder,
    relative_model_entry,
    write_forwarder,
)
from .locked import build_locked, check_bindings, contract_digest, resolve_cached
from .materialize import BEHAVIOR_KINDS, behavior_kind, materialize, verify_generated
from .package import verify_package
from .paths import inside, sha
from .resource_definition import facts_from_asset, generate_resource_definition
from .simulated_device import generate_simulated_device

__all__ = [
    "BEHAVIOR_KINDS",
    "ForwarderOptions",
    "behavior_kind",
    "generate_forwarder",
    "inside",
    "materialize",
    "relative_model_entry",
    "sha",
    "verify_generated",
    "verify_package",
    "write_forwarder",
    "build_locked", "check_bindings", "contract_digest", "resolve_cached",
    "generate_simulated_device",
    "generate_resource_definition", "facts_from_asset",
    "SITE_FRAME", "apply_site_map",
    "rails_from_asset", "rail_mount_from_asset", "mount_pose", "carrier_pose", "grid_at", "covered",
    "carrier_at", "check_layout",
]
