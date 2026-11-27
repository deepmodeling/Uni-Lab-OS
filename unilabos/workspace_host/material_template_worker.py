"""Isolated static compiler for workspace Material templates.

This module is intentionally executable in a short-lived Python process.  It
never imports author modules and never publishes a partial catalog; callers
receive either one complete catalog identity or structured diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from unilabos.package_manager import (
    PackageCompileError,
    WorkspaceSource,
    compile_package_source,
)

SCHEMA_VERSION = "unilab-material-template-validation/v1"


def validate_workspace(workspace: str | Path) -> dict[str, Any]:
    """Compile a workspace catalog without loading or publishing author code."""

    root = Path(workspace).expanduser().resolve(strict=True)
    try:
        catalog = compile_package_source(WorkspaceSource(root))
    except PackageCompileError as error:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "status": "invalid",
            "workspacePath": str(root),
            "diagnostics": [item.to_dict() for item in error.diagnostics],
        }
    document = catalog.to_dict()
    definitions = document.get("definitions") or {}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "valid",
        "workspacePath": str(root),
        "catalogDigest": catalog.catalog_digest,
        "templateRevision": catalog.catalog_digest,
        "counts": {
            "devices": len(definitions.get("devices") or []),
            "resources": len(definitions.get("resources") or []),
            "workflows": len(definitions.get("workflows") or []),
            "assets": len(document.get("assets") or []),
        },
        "diagnostics": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    output = Path(arguments.output).expanduser().resolve()
    try:
        result = validate_workspace(arguments.workspace)
    except BaseException as error:  # noqa: BLE001 - isolate the host boundary.
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "status": "invalid",
            "workspacePath": str(Path(arguments.workspace).expanduser().resolve()),
            "diagnostics": [
                {
                    "code": "template_validation_failed",
                    "message": str(error),
                }
            ],
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return 0 if result["status"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA_VERSION", "validate_workspace"]
