"""``--check_mode`` 的导入校验不能改写 AST 构建的动作 schema：校验的必须是运行时用的那一份。"""

from __future__ import annotations

import copy
import sys
import textwrap
from pathlib import Path

from unilabos.registry.registry import Registry


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_verify_keeps_typed_and_legacy_action_schemas(tmp_path: Path, monkeypatch) -> None:
    pkg = tmp_path / "parity_pkg"
    _write(pkg / "__init__.py", "")
    _write(
        pkg / "photometer.py",
        '''
        from __future__ import annotations

        from typing import Annotated, Optional, TypedDict

        from pydantic import Field

        from unilabos.registry.decorators import action, device


        class Reading(TypedDict):
            wavelength_nm: float
            absorbance: float


        @device(id="parity_photometer", category=["parity"])
        class ParityPhotometer:
            def __init__(self, **kwargs):
                pass

            @action(contract="typed", description="read")
            def read_absorbance(
                self, wavelength_nm: Annotated[float, Field(ge=190, le=1100)] = 600.0
            ) -> Reading:
                """Read.

                Args:
                    wavelength_nm[波长(nm)]: 波长
                """
                return Reading(wavelength_nm=wavelength_nm, absorbance=0.1)

            @action(description="warm up")
            def warm_up(self, target_c: float = 37.0, label: Optional[str] = None) -> dict:
                return {"success": True}
        ''',
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    registry = Registry()
    registry.setup(devices_dirs=[str(pkg)], external_only=True)
    actions = registry.device_type_registry["parity_photometer"]["class"]["action_value_mappings"]
    before = {name: copy.deepcopy(actions[name]["schema"]) for name in ("read_absorbance", "warm_up")}

    assert registry.verify_and_resolve_registry() == []

    after = registry.device_type_registry["parity_photometer"]["class"]["action_value_mappings"]
    assert after["read_absorbance"]["contract_kind"] == "typed"
    typed = after["read_absorbance"]["schema"]
    assert typed == before["read_absorbance"]
    assert typed["x-unilabos-action-contract"]["version"] == 2
    wavelength = typed["properties"]["goal"]["properties"]["wavelength_nm"]
    assert (wavelength["type"], wavelength["minimum"], wavelength["maximum"]) == ("number", 190, 1100)
    # 普通 JSON 命令同样保持 AST 构建的 goal/result 包装
    assert after["warm_up"]["schema"] == before["warm_up"]
    assert set(after["warm_up"]["schema"]["properties"]) >= {"goal"}
