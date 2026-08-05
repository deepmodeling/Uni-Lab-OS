from pathlib import Path
from importlib.util import find_spec

import pytest

import unilabos.devices.workstation.szlab_poly_studio as szlab_poly_studio


def test_szlab_poly_studio_imports_from_device_workstation_package():
    assert "SZLabPolyPLCDevice" in szlab_poly_studio.__all__
    assert "S1Workstation" in szlab_poly_studio.__all__
    assert "SZLabPolyStudioDeck" in szlab_poly_studio.__all__
    assert "SzlabMixerPumpDevice" in szlab_poly_studio.__all__
    assert "SzlabMixerPhotoShottingDevice" in szlab_poly_studio.__all__
    assert "SzlabMixerMagneticStirrerDevice" in szlab_poly_studio.__all__


def test_szlab_poly_studio_default_csv_resolves_inside_workstation_package():
    if find_spec("pylabrobot") is None:
        pytest.skip("pylabrobot 未安装，跳过依赖 PLC 模块导入的布局检查")

    from unilabos.devices.workstation.szlab_poly_studio.plc import _resolve_csv_path

    csv_path = Path(_resolve_csv_path(None))

    assert csv_path.name == "szlab_plc_0721.csv"
    assert csv_path.parent.parts[-4:] == (
        "unilabos",
        "devices",
        "workstation",
        "szlab_poly_studio",
    )
    assert csv_path.exists()


def test_szlab_poly_studio_latest_csv_supports_utf16_encoding():
    if find_spec("pylabrobot") is None:
        pytest.skip("pylabrobot 未安装，跳过依赖 PLC 模块导入的 CSV 检查")

    from unilabos.devices.workstation.szlab_poly_studio.plc import load_variable_names_from_csv

    csv_path = Path("unilabos/devices/workstation/szlab_poly_studio/szlab_plc_0721.csv")

    names = load_variable_names_from_csv(str(csv_path))

    assert "S05加工完成" in names
    assert "S05拍照结果" in names
    assert "S09测密度次数" in names
    assert "S09抽液天平读数[9]" in names
    assert "S09放液天平读数[9]" in names
    assert "S09数据清空" in names


def test_temporary_top_level_workstation_package_path_is_removed():
    removed_module_path = ".".join(
        ["unilabos", "workstation", "szlab_poly_studio"]
    )

    with pytest.raises(ModuleNotFoundError):
        __import__(removed_module_path)


def test_szlab_poly_studio_station_folders_use_station_prefixes():
    root = Path("unilabos/devices/workstation/szlab_poly_studio")
    expected_dirs = {
        "s04_magnetic_stirring",
        "s05_photoshotting",
        "s06_pump",
        "s08_decap",
        "s12_robot",
    }
    removed_dirs = {
        "magnetic_stirring",
        "photoshotting",
        "pump",
        "decap_s08",
        "robot",
    }

    for name in expected_dirs:
        assert (root / name).is_dir()
    for name in removed_dirs:
        assert not (root / name).exists()


def test_szlab_poly_studio_deck_builds_frontend_stack_status_from_sensor_groups():
    from unilabos.devices.workstation.szlab_poly_studio.stack_status import (
        build_stack_status,
    )

    status = build_stack_status(
        {
            "s10_liquid_reagent": {"1-1": True, "1-2": False},
            "powder_container": {"2-3": True},
        }
    )

    assert status["success"] is True
    assert status["schema"] == "szlab_poly_studio.stack_status.v1"
    assert status["stacks"]["s10_liquid_reagent"]["warehouse_name"] == "S10液体试剂瓶仓占位"
    assert status["stacks"]["s10_liquid_reagent"]["managed_resource"] == "reagent"
    assert status["stacks"]["s10_liquid_reagent"]["slots"]["1-1"]["occupied"] is True
    assert status["stacks"]["s10_liquid_reagent"]["slots"]["1-2"]["occupied"] is False
    assert status["stacks"]["s10_liquid_reagent"]["slots"]["1-1"]["reagent_id"] is None
    assert status["stacks"]["powder_container"]["slots"]["2-3"]["occupied"] is True
    assert "s2_tip" not in status["stacks"]
