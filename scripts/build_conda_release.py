"""构建默认无 ROS 包及显式选装包；不在构建过程中从 pip 隐式联网补依赖。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


# 公共 channel 缺失的纯 Python/字体包，仅 full 需要，不增加用户安装档位。
FULL_SUPPORT_RECIPES = (
    "opcua", "rinoh-typeface-dejavuserif", "rinoh-typeface-texgyrecursor",
    "rinoh-typeface-texgyreheros", "rinoh-typeface-texgyrepagella",
)

# ABI 升级时前置包必须来自同一次构建，不能依赖频道中的旧 cp312 产物。
EXTERNAL_RECIPES = {
    "opentrons-shared-data": "recipes/opentrons-shared-data/recipe.yaml",
    "msgs": "recipes/msgs/recipe.yaml",
    "msgs-humble": "recipes/msgs-humble/recipe.yaml",
}


def recipes(ros_distros: str = "", full: bool = False, extensions_only: bool = False) -> list[tuple[str, list[str]]]:
    distros = list(dict.fromkeys(x.strip() for x in ros_distros.split(",") if x.strip()))
    if any(x not in ("jazzy", "humble") for x in distros):
        raise ValueError("ros_distros 只能为空、jazzy、humble 或 jazzy,humble")
    if full and not distros:
        raise ValueError("build_full 必须显式选择 ros_distros")
    if extensions_only and not distros:
        raise ValueError("仅构建扩展必须显式选择 ros_distros，并先发布同版本默认包")
    result = [] if extensions_only else [
        (name, []) for name in ("opentrons-shared-data", "msgcenterpy", "pylabrobot", "mcp", "base")
    ]
    if full:
        result.extend((name, []) for name in FULL_SUPPORT_RECIPES)
    for distro in distros:
        suffix = "-humble" if distro == "humble" else ""
        result.append((f"msgs{suffix}", [f"robostack-{distro}"]))
        result.append((f"ros2{suffix}", [f"robostack-{distro}"]))
        if full:
            result.append((f"full{suffix}", [f"robostack-{distro}"]))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("build", "upload"))
    parser.add_argument("--platform", required=True, choices=("linux-64", "osx-64", "osx-arm64", "win-64"))
    parser.add_argument("--ros-distros", default="")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--extensions-only", action="store_true", help="复用已发布的默认包，仅构建/上传 ROS2/full 扩展")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--artifact-dir", default="conda-artifacts")
    parser.add_argument("--skip-existing", action="store_true", help="续跑时复用本地已有产物；调用方须先确认其测试通过")
    args = parser.parse_args()
    selected = recipes(args.ros_distros, args.full, args.extensions_only)
    output = Path(args.output_dir).resolve()
    if args.operation == "build":
        build_env = os.environ.copy()
        if os.name == "nt":
            # 仅为本次构建的 git 子进程开启长路径，不修改用户全局 git 配置。
            count = int(build_env.get("GIT_CONFIG_COUNT", "0"))
            build_env.update({"GIT_CONFIG_COUNT": str(count + 1),
                              f"GIT_CONFIG_KEY_{count}": "core.longpaths",
                              f"GIT_CONFIG_VALUE_{count}": "true"})
        for name, extra_channels in selected:
            recipe_path = EXTERNAL_RECIPES.get(name, f".conda/{name}/recipe.yaml")
            command = ["rattler-build", "build", "-r", recipe_path,
                       "--target-platform", args.platform, "--output-dir", str(output),
                       "-c", str(output), "-c", "uni-lab", "-c", "conda-forge"]
            for channel in extra_channels:
                command.extend(("-c", channel))
            if args.skip_existing:
                command.extend(("--skip-existing", "local"))
            subprocess.run(command, check=True, env=build_env)
        for subdir in ("noarch", args.platform):
            for package in (output / subdir).glob("*.conda"):
                target = Path(args.artifact_dir) / subdir / package.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(package, target)
        return

    token = os.environ["ANACONDA_API_TOKEN"]
    upload_env = {**os.environ, "ANACONDA_CLIENT_FORCE_STANDALONE": "1"}
    # noarch 在矩阵内重复构建；平台相关的 MCP 依赖也会被后续框架版本复用。
    # 这些已有坐标只跳过、不覆盖；框架平台包仍须发布新的版本/build。
    packages = sorted((output / "noarch").glob("*.conda")) + sorted((output / args.platform).glob("*.conda"))
    if not packages:
        raise RuntimeError("没有已构建的 .conda 产物，禁止写入 published 凭证")
    for package in packages:
        package_name = package.name.rsplit("-", 2)[0]
        if args.extensions_only and package_name not in {
            "unilabos-ros2", "unilabos-full", "ros-jazzy-unilabos-msgs",
            "ros-humble-unilabos-msgs", *FULL_SUPPORT_RECIPES,
        }:
            continue
        command = [sys.executable, "-m", "binstar_client.scripts.cli", "-t", token,
                   "upload", "--user", "uni-lab", "--register"]
        if package.parent.name == "noarch" or package_name == "mcp":
            command.append("--skip-existing")
        command.append(str(package))
        if subprocess.run(command, env=upload_env).returncode:
            raise RuntimeError(f"上传失败：{package.name}")


if __name__ == "__main__":
    main()
