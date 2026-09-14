"""Optional XDL-to-Uni-Lab workflow bridge.

The bridge translates portable XDL into the existing Uni-Lab workflow contract.
It does not start devices or alter native workflow execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .builder import build_workflow, validate_workflow
from .parser import parse_xdl
from .profile import StationProfile, load_station_profile


def build_xdl_workflow(
    xdl_path: str | Path, profile_path: str | Path, *, name: str | None = None
) -> dict[str, Any]:
    procedure = parse_xdl(xdl_path)
    profile = load_station_profile(profile_path)
    payload = build_workflow(procedure, profile, name=name or Path(xdl_path).stem)
    validate_workflow(payload, profile)
    return payload


def upload_xdl_workflow(
    xdl_path: str | Path,
    profile_path: str | Path,
    *,
    name: str | None = None,
    tags: list[str] | None = None,
    description: str = "",
    client: Any = None,
) -> dict[str, Any]:
    payload = build_xdl_workflow(xdl_path, profile_path, name=name)
    if client is None:
        from unilabos.app.web import http_client as client
    workflow_name = name or Path(xdl_path).stem
    response = client.workflow_import(
        name=workflow_name,
        workflow_uuid=payload["workflow_uuid"],
        workflow_name=workflow_name,
        nodes=payload["nodes"],
        edges=payload["edges"],
        tags=tags or [],
        description=description,
        published=False,
    )
    if response.get("code") != 0:
        raise RuntimeError(f"Workflow upload failed: {response}")
    return response


__all__ = [
    "StationProfile",
    "build_xdl_workflow",
    "build_workflow",
    "load_station_profile",
    "parse_xdl",
    "upload_xdl_workflow",
    "validate_workflow",
]
