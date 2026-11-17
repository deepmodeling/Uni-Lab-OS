"""驱动包里的相对导入：AST 扫描、typed 合同的模块作用域与工作流合同都按真实包结构解析。"""

from __future__ import annotations

import ast
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from unilabos.registry.ast_registry_scanner import _collect_imports, scan_directory
from unilabos.registry.module_scope import ModuleScopeError, resolve_module_scope
from unilabos.registry.workflow_contract import module_symbols


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


@pytest.fixture()
def relative_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "rel_pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "labware.py", "class ExampleDeck:\n    pass\n")
    _write(pkg / "devices" / "__init__.py", "")
    _write(pkg / "devices" / "helpers.py", "SCALE = 2.0\n")
    _write(
        pkg / "devices" / "photometer.py",
        '''
        from __future__ import annotations

        from typing import Annotated, TypedDict

        from pydantic import Field

        from unilabos.registry.decorators import action, device

        from ..labware import ExampleDeck
        from . import helpers
        from .helpers import SCALE as scale


        class Reading(TypedDict):
            absorbance: float


        @device(id="rel_photometer", category=["rel"])
        class RelPhotometer:
            def __init__(self, deck: ExampleDeck | None = None):
                self.deck = deck

            @action(contract="typed", description="read")
            def read(self, wavelength_nm: Annotated[float, Field(ge=190, le=1100)] = 600.0) -> Reading:
                return Reading(absorbance=wavelength_nm * scale * helpers.SCALE)
        ''',
    )
    return pkg


def test_scan_resolves_relative_imports_and_keeps_typed_actions(relative_pkg: Path, caplog) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(relative_pkg, python_path=relative_pkg.parent, executor=executor)

    assert "AST 扫描文件失败" not in caplog.text
    device = result["devices"]["rel_photometer"]
    assert device["module"] == "rel_pkg.devices.photometer:RelPhotometer"
    imports = device["import_map"]
    assert imports["ExampleDeck"] == "rel_pkg.labware:ExampleDeck"
    assert imports["helpers"] == "rel_pkg.devices:helpers"
    assert imports["scale"] == "rel_pkg.devices.helpers:SCALE"
    action = device["actions"]["read"]
    wavelength = action["canonical_schema"]["properties"]["goal"]["properties"]["wavelength_nm"]
    assert (wavelength["minimum"], wavelength["maximum"]) == (190, 1100)


def test_collect_imports_resolves_package_relative_imports() -> None:
    tree = ast.parse("from . import a\nfrom .b import c\nfrom ..d import e as f\n")
    # 普通模块：包是父路径
    assert _collect_imports(tree, "top.pkg.mod") == {"a": "top.pkg:a", "c": "top.pkg.b:c", "f": "top.d:e"}
    # __init__.py：包就是模块自己
    imports = _collect_imports(tree, "top.pkg", is_package=True)
    assert (imports["a"], imports["c"], imports["f"]) == ("top.pkg:a", "top.pkg.b:c", "top.d:e")


def test_module_scope_resolves_relative_imports() -> None:
    tree = ast.parse("from .labware import Deck\nfrom .. import shared\n")
    scope = resolve_module_scope(tree, module_name="top.pkg.driver")
    assert dict(scope.import_identities) == {"Deck": "top.pkg.labware:Deck", "shared": "top:shared"}

    package_scope = resolve_module_scope(tree, module_name="top.pkg", is_package=True)
    assert package_scope.import_identities["Deck"] == "top.pkg.labware:Deck"


def test_module_scope_rejects_relative_imports_beyond_the_top_level_package() -> None:
    tree = ast.parse("from ...outside import thing\n")
    with pytest.raises(ModuleScopeError):
        resolve_module_scope(tree, module_name="top.driver")


def test_workflow_contract_symbols_resolve_relative_imports() -> None:
    tree = ast.parse("from .models import Volume\nfrom typing import Annotated\n")
    imports, _records = module_symbols(tree, module_name="pkg.workflows")
    assert imports == {"Volume": "pkg.models:Volume", "Annotated": "typing:Annotated"}
