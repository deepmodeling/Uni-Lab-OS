"""ROS wire-name helpers that preserve the logical UniLabOS device ID."""

from __future__ import annotations

import re


_INVALID_ROS_NAME_CHARACTER = re.compile(r"[^A-Za-z0-9_]")


def ros_name_segment(value: str) -> str:
    """Return a valid ROS name token without changing the logical ID."""

    segment = _INVALID_ROS_NAME_CHARACTER.sub("_", str(value).strip())
    if not segment:
        raise ValueError("ROS name segment must not be blank")
    if segment[0].isdigit():
        segment = f"_{segment}"
    return segment


def ros_device_path(device_id: str) -> str:
    """Map a logical device ID to its ROS-safe relative namespace path."""

    value = str(device_id).strip()
    if value.startswith("/devices/"):
        value = value[len("/devices/") :]
    else:
        value = value.strip("/")
    segments = value.split("/") if value else []
    if not segments or any(not segment for segment in segments):
        raise ValueError("device_id must contain non-blank path segments")
    return "/".join(ros_name_segment(segment) for segment in segments)


def ros_device_namespace(device_id: str) -> str:
    """Return the absolute ROS namespace for a logical device ID."""

    return f"/devices/{ros_device_path(device_id)}"


def ros_device_node_name(device_id: str) -> str:
    """Return the ROS node token for the final logical device-ID segment."""

    return ros_device_path(device_id).split("/")[-1]


__all__ = [
    "ros_device_namespace",
    "ros_device_node_name",
    "ros_device_path",
    "ros_name_segment",
]
