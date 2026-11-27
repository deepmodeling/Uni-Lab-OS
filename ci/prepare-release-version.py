"""Apply a release tag's version to the disposable CI checkout.

The source repository keeps its development version.  This script only changes
the checkout used by a release job so the Conda package, Python distribution,
and installer all have the version selected by the Git tag.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TAG_PATTERN = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.-]+)?)$")


def replace_exactly_once(path: Path, pattern: str, replacement: str) -> None:
    content = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"Expected one version field in {path}, found {count}")
    path.write_text(updated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()

    match = TAG_PATTERN.fullmatch(args.tag)
    if not match:
        raise SystemExit("Release tag must use vMAJOR.MINOR.PATCH format, for example v0.3.0")
    version = match.group("version")
    repo = args.repo.resolve()

    replace_exactly_once(
        repo / "unilabos" / "__init__.py",
        r'^__version__\s*=\s*["\'][^"\']+["\']\s*$',
        f'__version__ = "{version}"',
    )
    replace_exactly_once(
        repo / "setup.py",
        r"^(\s*version\s*=\s*)['\"][^'\"]+['\"](,?)\s*$",
        rf"\g<1>'{version}'\g<2>",
    )
    for recipe in (
        repo / ".conda" / "base" / "recipe.yaml",
        repo / ".conda" / "environment" / "recipe.yaml",
        repo / "recipes" / "msgs" / "recipe.yaml",
    ):
        replace_exactly_once(recipe, r"^(\s*version\s*:\s*)[^\s#]+", rf"\g<1>{version}")
    replace_exactly_once(
        repo / ".conda" / "base" / "recipe.yaml",
        r"^(\s*-\s*(?:uni-lab::)?unilabos-env\s*==)\S+",
        rf"\g<1>{version}",
    )
    replace_exactly_once(
        repo / ".conda" / "environment" / "recipe.yaml",
        r"^([ \t]*-[ \t]*(?:uni-lab::)?ros-humble-unilabos-msgs)(?:[ \t]*==\S+)?[ \t]*$",
        rf"\g<1> =={version}",
    )
    # 所有平台都优先使用本次 tag 构建的本地包。
    for name in (".conda/base/recipe.yaml", ".conda/environment/recipe.yaml"):
        path = repo / name
        content = path.read_text(encoding="utf-8")
        content = content.replace("uni-lab::unilabos-env", "unilabos-env")
        content = content.replace("uni-lab::ros-humble-unilabos-msgs", "ros-humble-unilabos-msgs")
        path.write_text(content, encoding="utf-8")
    print(f"Prepared release version {version} from tag {args.tag}")


if __name__ == "__main__":
    main()
