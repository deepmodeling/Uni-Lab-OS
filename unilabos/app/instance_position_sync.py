"""生成设备包权威的物料（Material）相对位置更新命令。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

POSITION_FIELDS = (
    "position_x",
    "position_y",
    "position_z",
    "width",
    "length",
    "depth",
    "scale_x",
    "scale_y",
    "scale_z",
    "rotation_x",
    "rotation_y",
    "rotation_z",
)


class InstancePositionSyncError(ValueError):
    """既有物料身份或修订版本不足以安全同步位置。"""


def position_update_request(
    node: Mapping[str, Any],
    material: Mapping[str, Any],
    current_position: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """按设备包位置生成带并发保护的 Backend Edge 更新命令。

    Args:
        node: 已规范化的设备图节点，包含稳定本地身份和可选相对位置。
        material: Backend 返回的既有物料（Material）摘要，包含 UUID 和修订版本。
        current_position: Backend 物料图中的当前相对位置；尚未定位时为 ``None``。

    Returns:
        设备包未声明位置或位置一致时返回 ``None``；不一致时返回更新请求体。

    Raises:
        InstancePositionSyncError: 待更新物料缺少 UUID 或有效修订版本时抛出。
    """

    desired_position = node.get("relative_position")
    if desired_position is None:
        return None
    if not isinstance(desired_position, Mapping):
        raise InstancePositionSyncError("normalized relative position must be an object")
    if _positions_equal(current_position, desired_position):
        return None

    # material_uuid 是 Backend 物料聚合的稳定身份，也是位置更新和幂等命令的目标。
    material_uuid = str(material.get("uuid") or "").strip()
    if not material_uuid:
        raise InstancePositionSyncError("existing material has no UUID")
    # revision 是 Backend 乐观并发版本，阻止同步覆盖读取之后发生的其他写入。
    revision = material.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise InstancePositionSyncError(
            f"existing material {material_uuid} has no valid revision"
        )

    # position_digest 让同一物料、修订版本和设备包位置形成稳定幂等身份。
    position_digest = _position_digest(desired_position)
    return {
        "relative_position": dict(desired_position),
        "expected_revision": revision,
        "idempotency_key": (
            f"instance-position/{material_uuid}/{revision}/{position_digest}"
        ),
        "extension": {
            "source": "device_package_graph",
            "edge_local_id": str(node.get("id") or ""),
        },
    }


def _positions_equal(
    current_position: Mapping[str, Any] | None,
    desired_position: Mapping[str, Any],
) -> bool:
    """比较 Backend 当前位置与设备包规范位置的业务数值。

    Args:
        current_position: Backend 当前位置，可能包含 UUID、Shape 等非几何字段。
        desired_position: 设备包规范化后的十二轴几何字段。

    Returns:
        十二个位置、尺寸、缩放和旋转字段在浮点容差内一致时返回 ``True``。
    """

    if current_position is None:
        return False
    for field in POSITION_FIELDS:
        current_value = current_position.get(field)
        desired_value = desired_position.get(field)
        if (
            isinstance(current_value, bool)
            or isinstance(desired_value, bool)
            or not isinstance(current_value, (int, float))
            or not isinstance(desired_value, (int, float))
            or not math.isclose(
                float(current_value),
                float(desired_value),
                rel_tol=1e-9,
                abs_tol=1e-9,
            )
        ):
            return False
    return True


def _position_digest(position: Mapping[str, Any]) -> str:
    """计算设备包规范位置的稳定短摘要。

    Args:
        position: 已通过设备图几何校验的相对位置字段。

    Returns:
        规范 JSON 的 SHA-256 前二十四位十六进制摘要，用于幂等键。

    Raises:
        InstancePositionSyncError: 位置无法编码为有限 JSON 数值时抛出。
    """

    try:
        canonical_position = json.dumps(
            {field: position.get(field) for field in POSITION_FIELDS},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InstancePositionSyncError(
            "normalized relative position cannot be encoded"
        ) from exc
    return sha256(canonical_position).hexdigest()[:24]
