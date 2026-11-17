"""从同一源码凭证下的本地 Conda 产物安装、验证并打包；禁止覆盖式 pip 重装。"""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--channel", default="conda-artifacts")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--artifact-dir", default="dist-package")
    parser.add_argument("--local-test", action="store_true", help="验证未提交源码；不生成发布说明和源码 SHA 归档")
    args = parser.parse_args()
    if not args.local_test:
        checked_out = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if checked_out != args.source_sha:
            raise RuntimeError("发布源码 SHA 与 checkout 不一致")
        subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "unilabos", "scripts", ".conda",
                        "setup.py", "pyproject.toml", "MANIFEST.in"], check=True)
    tree = ast.parse(Path("unilabos/__init__.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets))
    channel = Path(args.channel).resolve()
    package = channel / args.platform / f"unilabos-{version}-py314_0.conda"
    if not package.is_file():
        raise RuntimeError(f"缺少当前源码对应的默认包：{package}；不回退到远端 latest")
    subprocess.run([sys.executable, "-m", "conda_index", str(channel)], check=True)
    conda = [os.environ["CONDA_PYTHON_EXE"], "-m", "conda"]
    prefix = str(Path(args.prefix).resolve())
    subprocess.run([*conda, "create", "-y", "-p", prefix, "--override-channels",
                    "-c", str(channel), "-c", "conda-forge", "-c", "uni-lab",
                    f"unilabos=={version}=py314_0", "conda-pack", "zstandard"], check=True)
    run = [*conda, "run", "--no-capture-output", "-p", prefix]
    subprocess.run([*run, "python", "-m", "pip", "check"], check=True)
    subprocess.run([*run, "python", "scripts/verify_installation.py", "--assert-no-ros"], check=True)
    subprocess.run([*run, "unilab", "--help"], check=True)
    target = Path(args.artifact_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run([*run, "conda-pack", "-p", prefix, "-o",
                    str(target / f"unilab-env-{args.platform}.tar.gz")], check=True)
    installer = "install_unilab.bat" if args.platform == "win-64" else "install_unilab.sh"
    for name in (installer, "verify_installation.py"):
        shutil.copy2(Path("scripts") / name, target / name)
    if args.local_test:
        print("本地测试包已验证；未生成发布说明或源码归档，不可作为发布产物。")
        return
    subprocess.run([sys.executable, "scripts/create_readme.py", args.platform,
                    args.source_sha, str(target / "README.txt")], check=True)
    subprocess.run(["git", "archive", "--format=zip", "-o", str(target / "source.zip"),
                    args.source_sha], check=True)


if __name__ == "__main__":
    main()
