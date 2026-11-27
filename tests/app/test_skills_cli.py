"""通过真实公共入口验证 Skill 安装及其文件边界。"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = "unilab-domain-repo-builder"
SOURCE = ROOT / "skills" / SKILL / "SKILL.md"
INSTALLED = Path(".agents", "skills", SKILL, "SKILL.md")


def run_skills(cwd, *args):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), environment.get("PYTHONPATH")) if value
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "unilabos.app.main", "skills", *args],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )


def test_skills_list_and_help_do_not_start_runtime(tmp_path):
    listing = run_skills(tmp_path, "list")
    assert listing.returncode == 0, listing.stderr
    assert SKILL in listing.stdout
    help_text = run_skills(tmp_path, "install", "--help")
    assert help_text.returncode == 0, help_text.stderr
    assert "--path" in help_text.stdout
    assert "~/.agents/skills/" in help_text.stdout
    assert list(tmp_path.iterdir()) == []


def test_skills_install_with_default_and_explicit_project(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    default = run_skills(tmp_path, "install", SKILL)
    assert default.returncode == 0, default.stderr
    target = project / INSTALLED
    arguments = ("install", SKILL, "--path", project.name)
    install = run_skills(tmp_path, *arguments)
    assert install.returncode == 0, install.stderr
    assert Path(install.stdout.strip()) == target
    assert target.read_bytes() == (tmp_path / INSTALLED).read_bytes() == SOURCE.read_bytes()

    repeat = run_skills(tmp_path, *arguments)
    assert repeat.returncode == 0, repeat.stderr
    assert not (project / "unilabos_data").exists()
    assert not (tmp_path / "unilabos_data").exists()


def test_skills_install_preserves_user_edits_and_rejects_legacy_duplicate(tmp_path):
    legacy = tmp_path / ".cursor" / "skills" / SKILL / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"user's legacy content")
    target = tmp_path / INSTALLED
    result = run_skills(tmp_path, "install", SKILL)
    assert result.returncode == 1
    assert ".cursor" in result.stderr
    assert not target.exists()
    assert legacy.read_bytes() == b"user's legacy content"

    legacy.unlink()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user's project content")
    result = run_skills(tmp_path, "install", SKILL)
    assert result.returncode == 1
    assert "SKILL.md" in result.stderr
    assert target.read_bytes() == b"user's project content"


def test_skills_rejects_invalid_name_and_project_path(tmp_path):
    assert run_skills(tmp_path, "install", "../unknown").returncode == 2
    result = run_skills(tmp_path, "install", SKILL, "--path", "missing")
    assert result.returncode == 1
    assert not (tmp_path / "missing").exists()
    assert list(tmp_path.iterdir()) == []
