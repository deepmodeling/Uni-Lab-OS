"""AIW-07 isolated Material template validation contracts."""

from __future__ import annotations

from pathlib import Path

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.model import WorkspacePaths


def _write_workspace(root: Path) -> Path:
    package = root / "template_lab"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "template-lab"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    root.joinpath("package.yaml").write_text(
        "package:\n  name: template_lab\nworkflows: []\n",
        encoding="utf-8",
    )
    source = package / "materials.py"
    source.write_text(
        "from unilabos.registry.decorators import resource\n\n"
        '@resource(id="plate", category=["container"])\n'
        "def make_plate(name: str):\n"
        "    return name\n",
        encoding="utf-8",
    )
    return source


def test_bad_template_isolated_compile_preserves_host_and_last_valid_scene(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _write_workspace(workspace)
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    try:
        valid = host._dispatch("material.template.validate", {})
        assert valid["status"] == "valid"
        assert valid["isolatedProcess"] is True
        assert valid["counts"]["resources"] == 1

        source.write_text("def broken(:\n", encoding="utf-8")
        invalid = host._dispatch("material.template.validate", {})

        assert invalid["status"] == "invalid"
        assert invalid["isolatedProcess"] is True
        assert invalid["lastValidScenePreserved"] is True
        assert invalid["diagnostics"][0]["code"] == "python_syntax_error"
        assert host.snapshot()["host"]["phase"] == "ready"
    finally:
        host.close()
