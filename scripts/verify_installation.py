#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Uni-Lab-OS Installation Verification Script
=========================================

This script verifies that Uni-Lab-OS and its dependencies are correctly installed.
Run this script after installing the conda-pack environment to ensure everything works.

Usage:
    python verify_installation.py [--auto-install]

    Options:
        --auto-install    Automatically install missing packages

    Or in the conda environment:
    conda activate unilab
    python verify_installation.py
"""

import sys
import sysconfig
import os
import argparse

# IMPORTANT: Set UTF-8 encoding BEFORE any other imports
# This ensures all subsequent imports (including unilabos) can output UTF-8 characters
if sys.platform == "win32":
    # Method 1: Reconfigure stdout/stderr to use UTF-8 with error handling
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    except (AttributeError, OSError):
        pass

    # Method 2: Set environment variable for subprocess and console
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # Method 3: Try to change Windows console code page to UTF-8
    try:
        import ctypes

        # Set console code page to UTF-8 (CP 65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except (ImportError, AttributeError, OSError):
        pass

# Now import other modules
import importlib
import importlib.util

# Use ASCII-safe symbols that work across all platforms
CHECK_MARK = "[OK]"
CROSS_MARK = "[FAIL]"


def check_package(package_name: str, display_name: str | None = None) -> bool:
    """
    Check if a package can be imported.

    Args:
        package_name: Name of the package to import
        display_name: Display name (defaults to package_name)

    Returns:
        bool: True if package is available
    """
    if display_name is None:
        display_name = package_name

    try:
        importlib.import_module(package_name)
        print(f"  {CHECK_MARK} {display_name}")
        return True
    except ImportError:
        print(f"  {CROSS_MARK} {display_name}")
        return False


def check_python_version() -> bool:
    """HostLink 与 ROS 2 统一要求普通 CPython 3.14（cp314，非 free-threaded）。"""
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"

    if (version.major == 3 and version.minor == 14
            and not sysconfig.get_config_var("Py_GIL_DISABLED")):
        print(f"  {CHECK_MARK} Python {version_str}")
        return True
    else:
        print(f"  {CROSS_MARK} Python {version_str} (requires the Python 3.14 / cp314 ABI)")
        return False


def check_material_catalog() -> bool:
    """必须实际创建物料：PLR 的可选导入可能把缺失的 Opentrons 依赖隐藏起来。"""
    try:
        import numpy as np
        from opentrons_shared_data.labware import labware_definition as ld
        from pylabrobot.resources.opentrons.plates import corning_96_wellplate_360ul_flat

        if int(np.__version__.split(".")[0]) != 2 or ld.trapezoid([1, 2, 3]) != 4:
            raise RuntimeError("NumPy 2 积分接口不正确")
        plate = corning_96_wellplate_360ul_flat("installation_check")
        if plate.num_items != 96:
            raise RuntimeError("Opentrons 标准孔板定义不完整")
        print(f"  {CHECK_MARK} NumPy 2 / Opentrons 96 孔板创建成功")
        return True
    except Exception as exc:
        print(f"  {CROSS_MARK} 物料库验证失败：{exc}")
        return False


def main():
    """Run all verification checks."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Verify Uni-Lab-OS installation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--auto-install",
        action="store_true",
        help="Automatically install missing packages",
    )
    parser.add_argument("--backend", choices=("hostlink", "ros2"), default="hostlink")
    parser.add_argument("--assert-no-ros", action="store_true", help="验证默认发行包未安装 ROS 运行时")
    parser.add_argument("--materials-only", action="store_true", help="只验证 PLR / Opentrons 物料依赖，不要求安装 UniLabOS")
    args = parser.parse_args()

    print("=" * 60)
    print("Uni-Lab-OS Installation Verification")
    print("=" * 60)
    if args.auto_install:
        print("Mode: Auto-install missing packages")
    else:
        print("Mode: Verification only")
    print()

    all_passed = True

    # Check Python version
    print("Checking Python version...")
    if not check_python_version():
        all_passed = False
    print()

    if args.backend == "ros2":
        for module in ("rclpy", "unilabos_msgs.msg", "unilabos_msgs.action"):
            all_passed = check_package(module) and all_passed
    elif args.assert_no_ros:
        for module in ("rclpy", "rosidl_runtime_py", "ament_index_python"):
            if importlib.util.find_spec(module) is not None:
                print(f"  {CROSS_MARK} 默认包不应包含 {module}")
                all_passed = False

    if args.materials_only:
        return 0 if check_material_catalog() and all_passed else 1

    # Run environment checker from unilabos
    print("Checking Uni-Lab-OS and dependencies...")
    try:
        from unilabos.utils.environment_check import check_environment

        print(f"  {CHECK_MARK} Uni-Lab-OS installed")

        # Check environment with optional auto-install
        # Set show_details=False to suppress detailed Chinese output that may cause encoding issues
        env_check_passed = check_environment(auto_install=args.auto_install, show_details=False)

        if env_check_passed:
            print(f"  {CHECK_MARK} All required packages available")
        else:
            all_passed = False
            print(f"  {CROSS_MARK} Some required packages are missing")
            if not args.auto_install:
                print("  Hint: Run with --auto-install to automatically install missing packages")
    except ImportError:
        print(f"  {CROSS_MARK} Uni-Lab-OS not installed")
        all_passed = False
    except Exception as e:
        all_passed = False
        print(f"  {CROSS_MARK} Environment check failed: {str(e)}")
    for module in ("unilabos.server.api.app", "unilabos.backend.hostlink.local_runtime"):
        all_passed = check_package(module) and all_passed
    all_passed = check_material_catalog() and all_passed
    print()

    # Summary
    print("=" * 60)
    print("Verification Summary")
    print("=" * 60)

    if all_passed:
        print(f"\n{CHECK_MARK} All checks passed! Your Uni-Lab-OS installation is ready.")
        print("\nNext steps:")
        print("  1. Review the documentation: docs/user_guide/launch.md")
        print("  2. Try the examples: docs/boot_examples/")
        print("  3. Configure your devices: unilabos_data/startup_config.json")
        return 0
    else:
        print(f"\n{CROSS_MARK} Some checks failed. Please review the errors above.")
        print("\nTroubleshooting:")
        print("  1. Ensure you're in the correct conda environment: conda activate unilab")
        print("  2. Check the installation documentation: docs/user_guide/installation.md")
        print("  3. Try reinstalling: pip install .")
        return 1


if __name__ == "__main__":
    sys.exit(main())
