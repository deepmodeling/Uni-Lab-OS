#!/usr/bin/env python3
"""
Development installation script for UniLabOS.
Auto-detects Chinese locale and uses appropriate mirror.

Usage:
    python scripts/dev_install.py
    python scripts/dev_install.py --no-mirror  # Force no mirror
    python scripts/dev_install.py --china      # Force China mirror
    python scripts/dev_install.py --skip-deps  # Skip pip dependencies installation

Flow:
    1. pip install -e . (install unilabos in editable mode)
    2. Detect Chinese locale
    3. Use uv to install pip dependencies from requirements.txt
    4. Special packages (like pylabrobot) are handled by environment_check.py at runtime
"""

import locale
import subprocess
import sys
import argparse
from pathlib import Path

# Tsinghua mirror URL
TSINGHUA_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"


def is_chinese_locale() -> bool:
    """
    Detect if system is in Chinese locale.
    Same logic as EnvironmentChecker._is_chinese_locale()
    """
    try:
        lang = locale.getdefaultlocale()[0]
        if lang and ("zh" in lang.lower() or "chinese" in lang.lower()):
            return True
    except Exception:
        pass
    return False


def run_command(cmd: list, description: str, retry: int = 2) -> bool:
    """Run command with retry support."""
    print(f"[INFO] {description}")
    print(f"[CMD] {' '.join(cmd)}")

    for attempt in range(retry + 1):
        try:
            result = subprocess.run(cmd, check=True, timeout=600)
            print(f"[OK] {description}")
            return True
        except subprocess.CalledProcessError as e:
            if attempt < retry:
                print(f"[WARN] Attempt {attempt + 1} failed, retrying...")
            else:
                print(f"[ERROR] {description} failed: {e}")
                return False
        except subprocess.TimeoutExpired:
            print(f"[ERROR] {description} timed out")
            return False
    return False


def install_editable(project_root: Path, use_mirror: bool) -> bool:
    """Install unilabos in editable mode using pip."""
    cmd = [sys.executable, "-m", "pip", "install", "-e", str(project_root)]
    if use_mirror:
        cmd.extend(["-i", TSINGHUA_MIRROR])

    return run_command(cmd, "Installing unilabos in editable mode")


def install_requirements_uv(requirements_file: Path, use_mirror: bool) -> bool:
    """Install pip dependencies using uv (installed via conda-forge::uv)."""
    cmd = ["uv", "pip", "install", "-r", str(requirements_file)]
    if use_mirror:
        cmd.extend(["-i", TSINGHUA_MIRROR])

    return run_command(cmd, "Installing pip dependencies with uv", retry=2)


def install_requirements_pip(requirements_file: Path, use_mirror: bool) -> bool:
    """Fallback: Install pip dependencies using pip."""
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(requirements_file)]
    if use_mirror:
        cmd.extend(["-i", TSINGHUA_MIRROR])

    return run_command(cmd, "Installing pip dependencies with pip", retry=2)


def check_uv_available() -> bool:
    """Check if uv is available (installed via conda-forge::uv)."""
    try:
        subprocess.run(["uv", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Development installation script for UniLabOS")
    parser.add_argument("--china", action="store_true", help="Force use China mirror (Tsinghua)")
    parser.add_argument("--no-mirror", action="store_true", help="Force use default PyPI (no mirror)")
    parser.add_argument(
        "--skip-deps", action="store_true", help="Skip pip dependencies installation (only install unilabos)"
    )
    parser.add_argument("--use-pip", action="store_true", help="Use pip instead of uv for dependencies")
    args = parser.parse_args()

    # Determine project root
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    requirements_file = project_root / "unilabos" / "utils" / "requirements.txt"

    if not (project_root / "setup.py").exists():
        print(f"[ERROR] setup.py not found in {project_root}")
        sys.exit(1)

    print("=" * 60)
    print("UniLabOS Development Installation")
    print("=" * 60)
    print(f"Project root: {project_root}")
    print()

    # Determine mirror usage based on locale
    if args.no_mirror:
        use_mirror = False
        print("[INFO] Mirror disabled by --no-mirror flag")
    elif args.china:
        use_mirror = True
        print("[INFO] China mirror enabled by --china flag")
    else:
        use_mirror = is_chinese_locale()
        if use_mirror:
            print("[INFO] Chinese locale detected, using Tsinghua mirror")
        else:
            print("[INFO] Non-Chinese locale detected, using default PyPI")

    print()

    # Step 1: Install unilabos in editable mode
    print("[STEP 1] Installing unilabos in editable mode...")
    if not install_editable(project_root, use_mirror):
        print("[ERROR] Failed to install unilabos")
        print()
        print("Manual fallback:")
        if use_mirror:
            print(f"  pip install -e {project_root} -i {TSINGHUA_MIRROR}")
        else:
            print(f"  pip install -e {project_root}")
        sys.exit(1)

    print()

    # Step 2: Install pip dependencies
    if args.skip_deps:
        print("[INFO] Skipping pip dependencies installation (--skip-deps)")
    else:
        print("[STEP 2] Installing pip dependencies...")

        if not requirements_file.exists():
            print(f"[WARN] Requirements file not found: {requirements_file}")
            print("[INFO] Skipping dependencies installation")
        else:
            # Try uv first (faster), fallback to pip
            if args.use_pip:
                print("[INFO] Using pip (--use-pip flag)")
                success = install_requirements_pip(requirements_file, use_mirror)
            elif check_uv_available():
                print("[INFO] Using uv (installed via conda-forge::uv)")
                success = install_requirements_uv(requirements_file, use_mirror)
                if not success:
                    print("[WARN] uv failed, falling back to pip...")
                    success = install_requirements_pip(requirements_file, use_mirror)
            else:
                print("[WARN] uv not available (should be installed via: mamba install conda-forge::uv)")
                print("[INFO] Falling back to pip...")
                success = install_requirements_pip(requirements_file, use_mirror)

            if not success:
                print()
                print("[WARN] Failed to install some dependencies automatically.")
                print("You can manually install them:")
                if use_mirror:
                    print(f"  uv pip install -r {requirements_file} -i {TSINGHUA_MIRROR}")
                    print("  or:")
                    print(f"  pip install -r {requirements_file} -i {TSINGHUA_MIRROR}")
                else:
                    print(f"  uv pip install -r {requirements_file}")
                    print("  or:")
                    print(f"  pip install -r {requirements_file}")

    print()
    print("=" * 60)
    print("Installation complete!")
    print("=" * 60)
    print()
    print("Note: Some special packages (like pylabrobot) are installed")
    print("automatically at runtime by unilabos if needed.")
    print()
    print("Verify installation:")
    print('  python -c "import unilabos; print(unilabos.__version__)"')
    print()
    print("If you encounter issues, you can manually install dependencies:")
    if use_mirror:
        print(f"  uv pip install -r unilabos/utils/requirements.txt -i {TSINGHUA_MIRROR}")
    else:
        print("  uv pip install -r unilabos/utils/requirements.txt")
    print()


if __name__ == "__main__":
    main()
