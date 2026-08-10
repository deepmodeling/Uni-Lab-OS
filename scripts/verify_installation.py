#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UniLabOS Installation Verification Script
=========================================

This script verifies that UniLabOS and its dependencies are correctly installed.
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
    """Check Python version."""
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"

    if version.major == 3 and version.minor == 12:
        print(f"  {CHECK_MARK} Python {version_str}")
        return True
    else:
        print(f"  {CROSS_MARK} Python {version_str} (requires the Python 3.12 / cp312 ABI)")
        return False


def main():
    """Run all verification checks."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Verify UniLabOS installation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--auto-install",
        action="store_true",
        help="Automatically install missing packages",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("UniLabOS Installation Verification")
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

    # Check ROS2 rclpy
    print("Checking ROS2 rclpy...")
    if not check_package("rclpy", "ROS2 rclpy"):
        all_passed = False
    print()

    # Run environment checker from unilabos
    print("Checking UniLabOS and dependencies...")
    try:
        from unilabos.utils.environment_check import check_environment

        print(f"  {CHECK_MARK} UniLabOS installed")

        # Check environment with optional auto-install
        # Set show_details=False to suppress detailed Chinese output that may cause encoding issues
        env_check_passed = check_environment(auto_install=args.auto_install, show_details=False)

        if env_check_passed:
            print(f"  {CHECK_MARK} All required packages available")
        else:
            print(f"  {CROSS_MARK} Some optional packages are missing")
            if not args.auto_install:
                print("  Hint: Run with --auto-install to automatically install missing packages")
    except ImportError:
        print(f"  {CROSS_MARK} UniLabOS not installed")
        all_passed = False
    except Exception as e:
        print(f"  {CROSS_MARK} Environment check failed: {str(e)}")
    print()

    # Summary
    print("=" * 60)
    print("Verification Summary")
    print("=" * 60)

    if all_passed:
        print(f"\n{CHECK_MARK} All checks passed! Your UniLabOS installation is ready.")
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
