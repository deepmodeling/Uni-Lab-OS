from pathlib import Path

import pytest

from unilabos.devices.workstation.szlab_poly_studio import (
    S1Workstation,
    SZLabPolyPLCDevice,
    SZLabPolyStudioDeck,
    SzlabMixerPhotoShottingDevice,
    SzlabMixerPumpDevice,
    SzlabMixerRobotDevice,
    SzlabMixerStirrerDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer import (
    SzlabMixerPhotoShottingDevice as MixerPackagePhotoShottingDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.plc import _resolve_csv_path


def test_szlab_poly_studio_imports_from_device_workstation_package():
    assert S1Workstation.__name__ == "S1Workstation"
    assert SZLabPolyPLCDevice.__name__ == "SZLabPolyPLCDevice"
    assert SZLabPolyStudioDeck.__name__ == "SZLabPolyStudioDeck"
    assert SzlabMixerPhotoShottingDevice.__name__ == "SzlabMixerPhotoShottingDevice"
    assert SzlabMixerPumpDevice.__name__ == "SzlabMixerPumpDevice"
    assert SzlabMixerRobotDevice.__name__ == "SzlabMixerRobotDevice"
    assert SzlabMixerStirrerDevice.__name__ == "SzlabMixerStirrerDevice"
    assert MixerPackagePhotoShottingDevice is SzlabMixerPhotoShottingDevice


def test_szlab_poly_studio_default_csv_resolves_inside_workstation_package():
    csv_path = Path(_resolve_csv_path(None))

    assert csv_path.name == "苏州实验室_0610.csv"
    assert csv_path.parent.parts[-4:] == (
        "unilabos",
        "devices",
        "workstation",
        "szlab_poly_studio",
    )
    assert csv_path.exists()


def test_temporary_top_level_workstation_package_path_is_removed():
    removed_module_path = ".".join(
        ["unilabos", "workstation", "szlab_poly_studio"]
    )

    with pytest.raises(ModuleNotFoundError):
        __import__(removed_module_path)
