"""面向 Coding Agent 的项目级 Skill 安装命令。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SKILLS = {
    "unilab-domain-repo-builder": "协助新建、迁移、修改和诊断 UniLab OS 设备包。",
}


def register_skills_subcommands(subparsers) -> None:
    parser = subparsers.add_parser("skills", help="管理 UniLab OS 随附的 Agent Skills")
    actions = parser.add_subparsers(dest="skills_action", required=True)
    actions.add_parser("list", help="列出随附的 Skills")
    install = actions.add_parser(
        "install",
        help="安装 Skill 到项目",
        epilog=(
            "用户级使用时，可手动将已安装的 Skill 目录复制到 Agent 的用户目录，"
            "例如 Codex 的 ~/.agents/skills/ 或 Cursor 的 ~/.cursor/skills/。"
        ),
    )
    install.add_argument("skill", choices=SKILLS)
    install.add_argument("--path", type=Path, default=Path("."), help="项目根目录（默认当前目录）")


def _read_skill(name: str) -> bytes:
    package = Path(__file__).resolve().parents[2]
    relative_path = Path(name) / "SKILL.md"
    packaged = package / "_bundled_skills" / relative_path
    if packaged.is_file():
        return packaged.read_bytes()
    source = package.parent / "skills" / relative_path
    try:
        return source.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"当前 UniLab OS 安装缺少 Skill 资源：{name}") from error


def _install_skill(name: str, project: Path) -> Path:
    root = project.resolve()
    if not root.is_dir():
        raise ValueError(f"项目根目录不存在或不是目录：{root}")

    legacy = root / ".cursor" / "skills" / name / "SKILL.md"
    if legacy.exists() or legacy.is_symlink():
        raise ValueError(f"请先移走同名的旧 Cursor Skill，再运行安装命令：{legacy}")

    target = root / ".agents" / "skills" / name / "SKILL.md"
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError(f"Skill 安装路径必须指向项目内的普通文件：{target}")

    content = _read_skill(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination = target.open("xb")
    except FileExistsError as error:
        if target.is_file() and target.read_bytes() == content:
            return target
        raise ValueError(f"已存在内容不同的 Skill，请先备份并移走已有文件：{target}") from error

    try:
        with destination:
            destination.write(content)
    except OSError:
        target.unlink(missing_ok=True)
        raise
    return target


def run_skills_command(args: dict[str, Any]) -> None:
    if args["skills_action"] == "list":
        for name, description in SKILLS.items():
            print(f"{name}\t{description}")
        return

    try:
        target = _install_skill(args["skill"], args["path"])
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f"unilab skills install: {error}") from error
    print(target)
