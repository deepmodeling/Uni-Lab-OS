"""计算 Python 单测镜像对应的依赖版本。"""

import ast
import hashlib
import json
import sys
import tomllib
from pathlib import Path


def requirement_lines(path: str) -> list[str]:
    return [
        line
        for raw_line in Path(path).read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]


def setup_requirements() -> list[str]:
    tree = ast.parse(Path("setup.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        call = statement.value if isinstance(statement, ast.Expr) else None
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "setup":
            for argument in call.keywords:
                if argument.arg == "install_requires":
                    requirements = ast.literal_eval(argument.value)
                    if isinstance(requirements, (list, tuple)) and all(
                        isinstance(requirement, str) for requirement in requirements
                    ):
                        return sorted(requirements)
    raise ValueError("setup.py 必须声明可直接读取的 install_requires")


def verify_requirements(lines: list[str]) -> None:
    from importlib.metadata import PackageNotFoundError, version

    from packaging.requirements import Requirement

    for line in lines:
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        if requirement.url or requirement.extras:
            raise ValueError(f"无法通过已安装的版本校验 {requirement}")
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            raise ValueError(f"镜像缺少依赖 {requirement}") from None
        if installed not in requirement.specifier:
            raise ValueError(f"镜像依赖不满足 {requirement}，已安装 {installed}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--verify-requirements":
        try:
            verify_requirements(requirement_lines(sys.argv[2]) + sys.argv[3:])
        except ValueError as error:
            sys.exit(str(error))
        sys.exit(0)

    pyproject = Path("pyproject.toml")
    build_system = {}
    if pyproject.is_file():
        build_system = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("build-system", {})
    dependencies = {
        "runtime": requirement_lines("unilabos/utils/requirements.txt"),
        "package": setup_requirements(),
        "lock": requirement_lines("ci/python-suite-requirements.lock"),
        "build_system": build_system,
    }
    if sys.argv[1:] == ["--verify"]:
        verify_requirements(dependencies["runtime"] + dependencies["package"] + build_system.get("requires", []))
    print(hashlib.sha256(json.dumps(dependencies, sort_keys=True).encode()).hexdigest())
