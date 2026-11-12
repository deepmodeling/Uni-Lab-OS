"""构建配套 wheel / 完整离线发行包；复用 Conda 补丁，不上传任何 Python 包源。"""

from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.request import urlopen
from zipfile import ZipFile

if __package__:
    from .install_wheel_release import environment_python, platform_id, sha256
else:
    from install_wheel_release import environment_python, platform_id, sha256

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True,
                   env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                        "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", "315532800")})


def read_version(root: Path) -> str:
    tree = ast.parse((root / "unilabos/__init__.py").read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))


def wheel_metadata(wheel: Path) -> tuple[str, str, list[str]]:
    with ZipFile(wheel) as archive:
        # setuptools 等包包含 vendored 依赖的 dist-info；只读 wheel 根目录的元数据。
        entries = [name for name in archive.namelist()
                   if name.count("/") == 1 and name.endswith(".dist-info/METADATA")]
        if len(entries) != 1:
            raise RuntimeError(f"wheel 元数据不唯一：{wheel.name}")
        metadata = BytesParser().parsebytes(archive.read(entries[0]))
    name = re.sub(r"[-_.]+", "-", metadata["Name"]).lower()
    version = metadata["Version"]
    if not re.fullmatch(r"[a-z0-9-]+", name) or not re.fullmatch(r"[A-Za-z0-9.+!-]+", version):
        raise RuntimeError(f"非法的包名或版本：{wheel.name}")
    return name, version, metadata.get_all("Requires-Dist", [])


def verify_companion_metadata(wheel: Path, name: str, spec: dict) -> None:
    """前置依赖必须写进 wheel 本身，不能由主包或完整锁文件碰巧补齐。"""
    actual_name, actual_version, dependencies = wheel_metadata(wheel)
    if (actual_name, actual_version) != (name, spec["version"]):
        raise RuntimeError(f"下游包版本不匹配：{wheel.name}")
    declared = {re.sub(r"\s+", "", dependency).lower() for dependency in dependencies}
    for requirement in spec.get("requires", []):
        if re.sub(r"\s+", "", requirement).lower() not in declared:
            raise RuntimeError(f"{wheel.name} 缺少必需的直接依赖：{requirement}")


def download_source(spec: dict, target: Path, cache: Path | None = None) -> None:
    """只接受固定哈希的源码，镜像也必须与原包逐字节一致。"""
    if not re.fullmatch(r"[0-9a-f]{64}", spec.get("sha256", "")):
        raise RuntimeError("源码缺少固定 SHA256，拒绝构建")
    cached = cache / f"{spec['sha256']}.tar.gz" if cache else None
    if cached and cached.is_file():
        if sha256(cached) != spec["sha256"]:
            raise RuntimeError(f"源码缓存 SHA256 不匹配：{cached}")
        shutil.copy2(cached, target)
        return
    urls = [spec["url"], *spec.get("mirrors", [])]
    failures = []
    for url in urls:
        if not url.startswith("https://"):
            raise RuntimeError("源码必须使用 HTTPS")
        try:
            with urlopen(url, timeout=60) as response, target.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            if sha256(target) != spec["sha256"]:
                raise RuntimeError(f"源码 SHA256 不匹配：{url}")
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, cached)
            return
        except OSError as exc:
            failures.append(str(exc))
    raise RuntimeError("下载源码失败：" + "; ".join(failures))


def extract_source(archive: Path, target: Path) -> Path:
    target.mkdir()
    with tarfile.open(archive) as stream:
        # Python 3.12 的 data filter 拒绝路径穿越及目录外链接。
        stream.extractall(target, filter="data")
    roots = list(target.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError("源码归档必须恰好包含一个项目根目录")
    return roots[0]


def build_dependencies(root: Path, work: Path, wheels: Path, cache: Path | None = None) -> dict:
    sources = tomllib.loads((root / "recipes/wheels.toml").read_text(encoding="utf-8"))["packages"]
    for name, spec in sources.items():
        archive = work / f"{name}.tar.gz"
        download_source(spec, archive, cache)
        source = extract_source(archive, work / name)
        for index, relative in enumerate(spec["patches"]):
            # Windows checkout 可能将补丁转成 CRLF；sdist 保持 LF，先规范临时副本。
            patch = work / f"{name}-{index}.patch"
            patch.write_text((root / relative).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            run("git", "apply", "--check", str(patch), cwd=source)
            run("git", "apply", str(patch), cwd=source)
        if spec.get("notice"):
            shutil.copy2(root / spec["notice"], source / "NOTICE")
        before = set(wheels.glob("*.whl"))
        run(sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheels), str(source), cwd=work)
        built = set(wheels.glob("*.whl")) - before
        if len(built) != 1:
            raise RuntimeError(f"{name} 未生成唯一 wheel")
        wheel = built.pop()
        verify_companion_metadata(wheel, name, spec)
        if spec.get("notice"):
            with ZipFile(wheel) as archive:
                notices = [item for item in archive.namelist() if item.endswith("/NOTICE")]
                licenses = [item for item in archive.namelist() if item.endswith("/LICENSE")]
                if not licenses or not any(archive.read(item) == (root / spec["notice"]).read_bytes()
                                           for item in notices):
                    raise RuntimeError(f"wheel 未携带许可证或修改说明：{wheel.name}")
    return sources


def verify_single_root(wheels: Path, prefix: Path, verifier: Path, requirement: str,
                       *, materials_only: bool = False) -> None:
    """只指定一个根依赖，在全新环境证明 pip 能自动带齐所有前置包。"""
    if prefix.exists():
        raise RuntimeError(f"验证环境必须是新目录：{prefix}")
    work = prefix.parent
    run(sys.executable, "-I", "-m", "venv", str(prefix), cwd=work)
    python = str(environment_python(prefix))
    run(python, "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
        "--no-index", "--only-binary=:all:", "--find-links", str(wheels), requirement, cwd=work)
    run(python, "-I", "-m", "pip", "check", cwd=work)
    run(python, "-I", str(verifier), "--assert-no-ros",
        *(["--materials-only"] if materials_only else []), cwd=work)
    if not materials_only:
        run(python, "-I", "-m", "unilabos.app.main", "--help", cwd=work)


def lock_wheels(wheels: Path) -> str:
    """冻结实际产物而非重新求解，拒绝仍会绕过 --no-index 联网的直接 URL 依赖。"""
    entries = {}
    for wheel in sorted(wheels.glob("*.whl")):
        name, version, dependencies = wheel_metadata(wheel)
        if name in entries:
            raise RuntimeError(f"wheelhouse 存在同名多份包：{name}")
        if any(re.search(r"@|https?://|file:", requirement) for requirement in dependencies):
            raise RuntimeError(f"离线包不允许直接 URL 依赖：{wheel.name}")
        entries[name] = f"{name}=={version} --hash=sha256:{sha256(wheel)}"
    if "unilabos" not in entries:
        raise RuntimeError("离线包缺少 unilabos wheel")
    return "\n".join(entries[name] for name in sorted(entries)) + "\n"


def snapshot_source(root: Path, work: Path, *, local_test: bool) -> tuple[Path, str]:
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    source = work / "unilabos-source"
    source.mkdir()
    paths = ["unilabos", "setup.py", "pyproject.toml", "MANIFEST.in", "LICENSE"]
    if local_test:
        shutil.copytree(root / "unilabos", source / "unilabos",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        for name in paths[1:]:
            shutil.copy2(root / name, source / name)
    else:
        # release 不使用工作区缓存、未提交源码或另一个 HEAD。
        changed = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all", "--",
                                           *paths, "scripts", "recipes", ".conda", ".github/workflows"],
                                          cwd=root, text=True)
        if changed.strip():
            raise RuntimeError("发布源码有未提交修改；本地验证请显式使用 --local-test")
        archive = work / "source.tar"
        run("git", "archive", "--format=tar", "-o", str(archive), source_sha, *paths, cwd=root)
        with tarfile.open(archive) as stream:
            stream.extractall(source, filter="data")
    return source, source_sha


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist/wheel-release"), help="必须是新的或空的目录")
    parser.add_argument("--dependencies-only", action="store_true", help="只构建配套依赖，供源码开发/CI 的 --find-links 使用")
    parser.add_argument("--local-test", action="store_true", help="允许未提交源码，产物标记 local-test，不能正式发布")
    parser.add_argument("--source-cache", type=Path, default=Path("dist/wheel-source-cache"), help="按源码哈希缓存下载，不缓存校验结果")
    parser.add_argument("--release-tag", default="", help="正式 Release 时校验 v<版本> 标签")
    args = parser.parse_args()
    if args.dependencies_only and not (3, 12) <= sys.version_info[:2] < (3, 15):
        raise RuntimeError("配套依赖构建支持 Python 3.12–3.14")
    if not args.dependencies_only and sys.version_info[:2] != (3, 12):
        raise RuntimeError("必须用 Python 3.12 构建默认发行包")
    version = read_version(ROOT)
    if args.release_tag and (args.local_test or args.release_tag != f"v{version}"):
        raise RuntimeError("Release 标签与源码版本不一致，或试图发布 local-test 产物")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"输出目录非空，不覆盖已有产物：{output}")
    output.mkdir(parents=True, exist_ok=True)
    wheels = output / "wheelhouse"
    wheels.mkdir()
    with tempfile.TemporaryDirectory(prefix="unilab-wheel-") as temporary:
        work = Path(temporary)
        source, source_sha = (None, None) if args.dependencies_only else snapshot_source(ROOT, work, local_test=args.local_test)
        sources = build_dependencies(ROOT, work, wheels, args.source_cache.resolve())
        if args.dependencies_only:
            print(f"配套依赖已生成：{wheels}；安装源码时使用 --find-links 指向此目录")
            return
        run(sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheels), str(source), cwd=work)
        main_wheels = [path for path in wheels.glob("*.whl") if wheel_metadata(path)[:2] == ("unilabos", version)]
        if len(main_wheels) != 1:
            raise RuntimeError("当前源码没有生成唯一的 UniLabOS wheel")
        # 将所有间接依赖也变成 wheel，目标用户无需 Git / 编译器 / Conda / 网络。
        run(sys.executable, "-m", "pip", "wheel", "--wheel-dir", str(wheels), "--find-links", str(wheels),
            str(main_wheels[0]), cwd=work)
        (output / "requirements.lock").write_text(lock_wheels(wheels), encoding="utf-8")
        for name in ("install_wheel_release.py", "verify_installation.py"):
            shutil.copy2(ROOT / "scripts" / name, output / name)
        shutil.copy2(ROOT / "recipes/wheels.toml", output / "wheel-sources.toml")
        # 保留发行所用补丁与 NOTICE，避免只分发一个无法追溯的二进制文件。
        for spec in sources.values():
            for relative in [*spec["patches"], *([spec["notice"]] if spec.get("notice") else [])]:
                target = output / "sources" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
        manifest = {"schema_version": 1, "kind": "offline-wheelhouse", "version": version,
                    "source_sha": source_sha, "local_test": args.local_test, "python": "3.12",
                    "platform": platform_id(), "verified": False,
                    "files": {path.relative_to(output).as_posix(): sha256(path)
                              for path in sorted(output.rglob("*")) if path.is_file()}}
        manifest_path = output / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # 全量锁文件会掩盖缺失的依赖边；PLR 和主包必须分别单独安装验收。
        roots = [f"pylabrobot=={sources['pylabrobot']['version']}", f"unilabos=={version}"]
        for index, requirement in enumerate(roots):
            verify_single_root(wheels, work / f"verify-root-{index}", output / "verify_installation.py",
                               requirement, materials_only=index == 0)
        # 安装入口自身就是验收用例：在仓库外创建干净环境，运行实际物料工厂和 CLI。
        run(sys.executable, "-I", str(output / "install_wheel_release.py"), "--prefix", str(work / "verify-env"), cwd=work)
        manifest["verified_install_roots"] = roots
        manifest["verified"] = True
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    label = "-local-test" if args.local_test else ""
    archive_base = output.parent / f"unilabos-{version}-wheelhouse-{platform_id()}{label}"
    if Path(str(archive_base) + ".zip").exists():
        raise RuntimeError(f"发行 ZIP 已存在，不会覆盖：{archive_base}.zip")
    archive_path = Path(shutil.make_archive(str(archive_base), "zip", root_dir=output))
    Path(str(archive_path) + ".sha256").write_text(f"{sha256(archive_path)}  {archive_path.name}\n", encoding="utf-8")
    print(f"完整离线包已验证：{archive_path}；未上传到 PyPI 或其他服务器。")


if __name__ == "__main__":
    main()
