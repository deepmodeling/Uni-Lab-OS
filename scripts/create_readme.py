#!/usr/bin/env python3
"""
Create Distribution Package README
===================================

Generate README.txt for conda-pack distribution packages.

Usage:
    python create_readme.py <platform> <branch> <output_file>

Arguments:
    platform: Platform identifier (win-64, linux-64, osx-64, osx-arm64)
    branch: Git branch name
    output_file: Output file path (e.g., dist-package/README.txt)

Example:
    python create_readme.py win-64 dev dist-package/README.txt
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_readme_content(platform: str, branch: str) -> str:
    """
    Generate README content for the specified platform.

    Args:
        platform: Platform identifier
        branch: Git branch name

    Returns:
        str: README content
    """
    # Get current UTC time
    build_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Determine platform-specific content
    is_windows = platform == "win-64"

    if is_windows:
        archive_ext = "zip"
        install_script = "install_unilab.bat"
        platform_instructions = """Windows:
  1. Extract the downloaded ZIP file
  2. Double-click install_unilab.bat (or run in cmd)
  3. Follow the prompts"""
    else:
        archive_ext = "tar.gz"
        install_script = "install_unilab.sh"
        platform_name = {"linux-64": "linux-64", "osx-64": "osx-64", "osx-arm64": "osx-arm64"}.get(platform, platform)
        platform_instructions = f"""macOS/Linux:
  1. Download and extract unilab-pack-{platform_name}.tar.gz
  2. Run: bash install_unilab.sh
  3. Follow the prompts
  
  Alternative (if downloaded from GitHub Actions):
  1. Extract the artifact ZIP file
  2. Extract unilab-pack-{platform_name}.tar.gz inside
  3. Run: bash install_unilab.sh"""

    # Generate README content
    readme = f"""UniLabOS Conda-Pack Environment
================================

This package contains a pre-built UniLabOS environment.

Installation Instructions:
--------------------------

{platform_instructions}

The installation script will:
  - Automatically find your conda installation
  - Extract the environment to conda's envs/unilab directory
  - Run conda-unpack to finalize setup

After installation:
  conda activate unilab
  python verify_installation.py

Verification:
-------------

The verify_installation.py script will check:
  - Python version (3.11.11)
  - ROS2 rclpy installation
  - UniLabOS installation and dependencies

If all checks pass, you're ready to use UniLabOS!

Package Contents:
-----------------

  - {install_script} (automatic installation script)
  - unilab-env-{platform}.tar.gz (packed conda environment)
  - verify_installation.py (environment verification tool)
  - README.txt (this file)

Build Information:
------------------

  Branch:   {branch}
  Platform: {platform}
  Python:   3.11.11
  Date:     {build_date}

Troubleshooting:
----------------

If installation fails:

  1. Ensure conda or mamba is installed
     Check: conda --version

  2. Verify you have sufficient disk space
     Required: ~5-10 GB after extraction

  3. Check installation permissions
     You need write access to conda's envs directory

  4. For detailed logs, run the install script from terminal

For more help:
  - Documentation: docs/user_guide/installation.md
  - Quick Start: QUICK_START_CONDA_PACK.md
  - Issues: https://github.com/deepmodeling/Uni-Lab-OS/issues

License:
--------

UniLabOS is licensed under GPL-3.0-only.
See LICENSE file for details.

Repository: https://github.com/deepmodeling/Uni-Lab-OS
"""

    return readme


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate README.txt for conda-pack distribution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python create_readme.py win-64 dev dist-package/README.txt
  python create_readme.py linux-64 main dist-package/README.txt
        """,
    )

    parser.add_argument("platform", choices=["win-64", "linux-64", "osx-64", "osx-arm64"], help="Platform identifier")

    parser.add_argument("branch", help="Git branch name")

    parser.add_argument("output_file", help="Output file path")

    args = parser.parse_args()

    try:
        # Generate README content
        readme_content = get_readme_content(args.platform, args.branch)

        # Create output directory if needed
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write README file
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(readme_content)

        print(f"  README.txt created: {output_path}")
        print(f"  Platform: {args.platform}")
        print(f"  Branch:   {args.branch}")

        return 0

    except Exception as e:
        print(f"Error creating README: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
