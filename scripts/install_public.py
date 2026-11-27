#!/usr/bin/env python3
"""从公开源码安装 Uni-Lab OS 和与之配套的控制台。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_TAG = "v1.1.0"
FRONTEND_ARCHIVE = f"developer-web-{FRONTEND_TAG}.tar.gz"
FRONTEND_RELEASE = (
    "https://uni-lab.oss-cn-zhangjiakou.aliyuncs.com/uni-lab-fe"
    f"/developer-web/{FRONTEND_TAG}"
)
# 与正式发布清单和 .sha256 文件核对过的版本摘要。
FRONTEND_SHA256 = "1a1e1b455b088b421103ef895532c3581975c18038671fd721832824347e0fc0"


def extract_static_file(member: tarfile.TarInfo, directory: str) -> tarfile.TarInfo | None:
    if not (member.isfile() or member.isdir()):
        raise ValueError(f"前端归档包含非常规文件：{member.name}")
    return tarfile.data_filter(member, directory)


def install_frontend(destination: Path) -> None:
    """校验并解包发布产物，验证通过后才替换现有控制台。"""
    with urlopen(f"{FRONTEND_RELEASE}/manifest.json", timeout=30) as response:
        manifest = json.loads(response.read(16_384))
    expected = {
        "schemaVersion": 1,
        "app": "developer-web",
        "tag": FRONTEND_TAG,
        "entry": "index.html",
        "file": FRONTEND_ARCHIVE,
        "sha256": FRONTEND_SHA256,
    }
    if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError(f"OSS 发布清单与固定版本 {FRONTEND_TAG} 不匹配")
    size = manifest.get("size")
    if type(size) is not int or size <= 0:
        raise ValueError("OSS 发布清单缺少有效的归档大小")

    with urlopen(f"{FRONTEND_RELEASE}/{FRONTEND_ARCHIVE}", timeout=30) as response:
        content = response.read(size + 1)
    if len(content) != size or hashlib.sha256(content).hexdigest() != FRONTEND_SHA256:
        raise ValueError("前端归档大小或 SHA-256 校验失败")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".console-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        archive = stage / FRONTEND_ARCHIVE
        archive.write_bytes(content)
        extracted = stage / "console"
        with tarfile.open(archive, mode="r:gz") as package:
            package.extractall(extracted, filter=extract_static_file)
        if not (extracted / "index.html").is_file() or not (extracted / "assets").is_dir():
            raise ValueError("前端归档缺少 index.html 或 assets/ 目录")

        previous = stage / "previous"
        if destination.exists():
            destination.replace(previous)
        try:
            extracted.replace(destination)
        except OSError:
            if previous.exists() and not destination.exists():
                previous.replace(destination)
            raise


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    if sys.version_info < (3, 11) or not hasattr(tarfile, "data_filter"):
        print("请激活 Python 3.11.14 或更新版本的 Uni-Lab OS 环境。", file=sys.stderr)
        return 1
    destination = ROOT / "unilabos/app/web/static/console"
    try:
        print(f"正在下载并校验控制台 {FRONTEND_TAG}…", flush=True)
        install_frontend(destination)
        requirements = ROOT / "unilabos/utils/requirements.txt"
        # 将已校验的控制台静态文件随 Python 包一起安装。
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements), str(ROOT)],
            cwd=ROOT,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        print(f"Python 包安装失败（pip 退出码 {error.returncode}）。", file=sys.stderr)
        return error.returncode
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"开源版安装失败：{error}", file=sys.stderr)
        return 1

    print(f"Uni-Lab OS 和控制台 {FRONTEND_TAG} 已安装。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
