"""按原始字节导出 Windows 构建脚本，避免 PowerShell 管道转码。"""

from __future__ import annotations

import argparse
import codecs
import subprocess
from pathlib import Path

CONTROL_SCRIPTS = (
    "build-runtime-windows.ps1",
    "upload-oss.py",
    "prepare-release-version.py",
    "check-oss-release.py",
    "notify-feishu-release.py",
    "oss-public-url.py",
)


def export_scripts(repo: Path, ref: str) -> None:
    scripts: dict[str, bytes] = {}
    for name in CONTROL_SCRIPTS:
        content = subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:ci/{name}"])
        if name.endswith(".py"):
            # 先校验全部 Python 源码，失败时不覆盖工作区中的任何脚本。
            compile(content, f"{ref}:ci/{name}", "exec")
        else:
            # Windows PowerShell 5.1 需要 BOM 才能正确识别含中文的 UTF-8 脚本。
            content.decode("utf-8-sig")
            if not content.startswith(codecs.BOM_UTF8):
                content = codecs.BOM_UTF8 + content
        scripts[name] = content

    for name, content in scripts.items():
        (repo / "ci" / name).write_bytes(content)
    print(f"已按原始字节导出并校验 {len(scripts)} 个构建脚本：{ref}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args()
    export_scripts(args.repo.resolve(), args.ref)


if __name__ == "__main__":
    main()
