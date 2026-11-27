"""资产交付（Asset Delivery）：把固定工程包落到领域包模型目录，并在模型之间共享相同贴图。"""

from .delivery import PROFILES, AssetDeliveryError, check_materialized, delivery_files, materialize_lock
from .shared_textures import SHARED_TEXTURE_DIR, load_manifest, share_textures

__all__ = [
    "AssetDeliveryError",
    "PROFILES",
    "SHARED_TEXTURE_DIR",
    "check_materialized",
    "delivery_files",
    "load_manifest",
    "materialize_lock",
    "share_textures",
]
