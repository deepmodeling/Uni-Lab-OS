"""软件包归档不得携带 Workbench 私有运行状态。"""

from __future__ import annotations

import tarfile

from unilabos.package_manager.package_distribution.archive import build_archive


def test_source_archive_excludes_workbench_and_agent_runtime_trees(tmp_path) -> None:
    package_root = tmp_path / "portable-lab"
    package_root.mkdir()
    package_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "portable-lab"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    private_root = package_root / ".unilabos"
    private_root.mkdir()
    private_root.joinpath("session.json").write_text("secret-runtime-state", encoding="utf-8")
    for native_root in (".claude", ".codex"):
        native_skill = package_root / native_root / "skills" / "runtime-skill"
        native_skill.parent.mkdir(parents=True)
        native_skill.symlink_to(private_root / "session.json")
        package_root.joinpath(native_root, "settings.json").write_text(
            '{"kept": true}\n',
            encoding="utf-8",
        )
    archive = tmp_path / "portable-lab.tar.gz"

    build_archive(package_root, archive)

    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
    assert any(name.endswith("pyproject.toml") for name in names)
    assert not any(".unilabos" in name.split("/") for name in names)
    assert not any(
        "/.claude/skills" in name or "/.codex/skills" in name
        for name in names
    )
    assert any(name.endswith(".claude/settings.json") for name in names)
    assert any(name.endswith(".codex/settings.json") for name in names)
