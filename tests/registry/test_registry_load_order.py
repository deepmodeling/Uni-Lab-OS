"""注册表加载有序、确定：同样的输入在任何进程里得到同一份注册表（条目顺序、列表顺序与取值）。

两个子进程用不同的 ``PYTHONHASHSEED`` 建核心内置注册表，外加一个返回 TypedDict 的小驱动包，
JSON 转储（不排序键）必须逐字节相同。以前 AST 扫描与 YAML 加载按线程完成先后合并、资源 YAML
按文件系统顺序遍历，注册表条目顺序每次启动都可能不同；TypedDict 的 ``required`` 曾随哈希种子变化（#388）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_DUMP = r"""
import json
import sys

from unilabos.registry.registry import build_registry

registry = build_registry(devices_dirs=[sys.argv[1]])
payload = {
    "devices": registry.device_type_registry,
    "resources": registry.resource_type_registry,
    "workflows": registry.workflow_registry,
    "device_info": registry.obtain_registry_device_info(),
    "resource_info": registry.obtain_registry_resource_info(),
    "failures": registry.check_failures(),
}
sys.stdout.write("\nREGISTRY_DUMP=" + json.dumps(payload, default=str, ensure_ascii=False) + "\n")
"""

_DRIVER = """
from typing import NotRequired, TypedDict

from unilabos.registry.decorators import action, device


class Reading(TypedDict):
    wavelength_nm: float
    absorbance: float
    transmittance: float
    sample_id: str
    blank_id: NotRequired[str]
    temperature_c: float


@device(id="load_order_reader", category=["load_order"])
class LoadOrderReader:
    def __init__(self, **kwargs):
        pass

    @action(description="read")
    def read(self, wavelength_nm: float = 600.0) -> Reading:
        return Reading(
            wavelength_nm=wavelength_nm, absorbance=0.1, transmittance=0.9, sample_id="s", temperature_c=25.0,
        )
"""


def _dump(package: Path, seed: str, home: Path) -> str:
    home.mkdir()
    env = {
        **os.environ,
        "PYTHONHASHSEED": seed,
        "PYTHONIOENCODING": "utf-8",
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _DUMP, str(package)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return next(
        line.removeprefix("REGISTRY_DUMP=")
        for line in completed.stdout.splitlines()
        if line.startswith("REGISTRY_DUMP=")
    )


def test_registry_is_identical_across_hash_seeds(tmp_path: Path) -> None:
    package = tmp_path / "load_order_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "reader.py").write_text(textwrap.dedent(_DRIVER), encoding="utf-8")

    first = _dump(package, "1", tmp_path / "home1")
    second = _dump(package, "2", tmp_path / "home2")

    assert first == second
    payload = json.loads(first)
    assert payload["failures"] == []
    result = payload["devices"]["load_order_reader"]["class"]["action_value_mappings"]["read"]["schema"][
        "properties"
    ]["result"]
    assert result["required"] == ["wavelength_nm", "absorbance", "transmittance", "sample_id", "temperature_c"]
    # 上报列表跟注册表同序，核心内置条目也在里面
    assert [item["id"] for item in payload["device_info"]] == list(payload["devices"])
    assert payload["resources"]
