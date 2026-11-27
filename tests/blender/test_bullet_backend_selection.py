"""``bullet_native`` 的后端选择测试：auto → standalone → bpy，失败不静默冒充。

本文件不需要 Blender：普通解释器里 ``bpy`` 不可导入，所以 ``auto`` 只能选中独立
Bullet 库或抛出汇总了三条路径原始错误的 ``RuntimeError``。独立库缺失时按
``UNILABOS_BULLET_STANDALONE_REQUIRE`` 决定跳过还是失败（与 conftest 一致）。
"""

import importlib
import json
import os
import sys

import pytest

from unilabos.simulation.bullet_standalone import loader

ENV_KEYS = (
    "UNILABOS_BULLET_BACKEND",
    "UNILABOS_BULLET_HEADERS",
    "UNILABOS_BULLET_CACHE",
    "UNILABOS_BULLET_STANDALONE",
)
PINNED_NATIVE = "d13f752e3b9c4f8c261cda552b1021f8bcc0382c"


def reload_native(env=None):
    """按给定环境变量重新导入 ``bullet_native``，返回新模块并还原环境。"""
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in (env or {}).items():
            os.environ[key] = value
        importlib.invalidate_caches()
        sys.modules.pop("unilabos.simulation.bullet_native", None)
        return importlib.import_module("unilabos.simulation.bullet_native")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def has_bpy():
    """当前解释器能否导入 bpy（决定 auto 的最后一层回退是否可用）。"""
    try:
        return importlib.util.find_spec("bpy") is not None
    except (ImportError, ValueError):
        return False


def test_auto_selects_standalone_when_native_unavailable(abi):
    """无 native 时必须选独立库，并在清单里写 backend/library_path/abi。"""
    module = reload_native({"UNILABOS_BULLET_BACKEND": "auto"})

    assert module.BACKEND == "standalone"
    # 原生桥在普通解释器里因缺少 bpy 失败；该失败必须被记录而不是丢弃。
    assert module.NATIVE_ERROR is not None
    assert module.REQUESTED_BACKEND == "auto"
    assert module.PIN.startswith("standalone:")
    assert module.LIBRARY_PATH == abi.path

    manifest = json.loads(module.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["backend"] == "standalone"
    assert manifest["library_path"] == str(abi.path)
    assert manifest["abi"] == abi.abi_version()
    assert manifest["scalar_bytes"] == 8
    assert manifest["blender_version"] is None
    assert manifest["native_pin"] == PINNED_NATIVE
    assert "native_error" in manifest


def test_standalone_exposes_the_bullet_usd_surface(abi):
    """``bullet_usd`` 用 ``from .bullet_native import *``，依赖这些名字存在。"""
    module = reload_native({"UNILABOS_BULLET_BACKEND": "standalone"})
    expected = {
        "B", "E", "C", "V3", "V4", "Contact", "World",
        "pose", "velocity", "quat", "np", "P", "F", "I",
    }
    assert expected.issubset(set(dir(module)))
    assert module.B is module.E
    assert module.World().tick == 0


def test_explicit_backend_choices_are_honoured(abi):
    """显式 native/bpy 不可用时必须报错，不能静默退到可用后端。"""
    with pytest.raises(Exception) as native_error:
        reload_native({"UNILABOS_BULLET_BACKEND": "native"})
    assert isinstance(native_error.value, (ImportError, KeyError, RuntimeError, OSError))

    if not has_bpy():
        with pytest.raises(RuntimeError) as bpy_error:
            reload_native({"UNILABOS_BULLET_BACKEND": "bpy"})
        assert "bpy" in str(bpy_error.value)

    with pytest.raises(RuntimeError, match="auto/native/standalone/bpy"):
        reload_native({"UNILABOS_BULLET_BACKEND": "pybullet"})


def test_auto_without_any_backend_reports_every_error(monkeypatch):
    """独立库也不可用且没有 bpy 时，错误信息必须同时给出三条路径的原因。"""
    if has_bpy():
        pytest.skip("Blender 解释器里 auto 会回退 bpy")

    def unavailable(**_kwargs):
        raise loader.LoaderError("独立库不可用（测试注入）")

    monkeypatch.setattr(loader, "load", unavailable)
    with pytest.raises(RuntimeError) as captured:
        reload_native({"UNILABOS_BULLET_BACKEND": "auto"})
    message = str(captured.value)
    assert "native=" in message and "standalone=" in message and "bpy=" in message
    assert "测试注入" in message


def test_availability_matches_loader(abi):
    """测试前提：``loader`` 认为独立库可用时 auto 才应该选中 standalone。"""
    available, reason = loader.availability()
    assert available, reason
