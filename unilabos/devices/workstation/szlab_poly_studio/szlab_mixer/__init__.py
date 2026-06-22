"""SZLab virtual mixer workstation devices."""

from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.photoshotting import (
    SzlabMixerPhotoShottingDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.pump import (
    SzlabMixerPumpDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.robot import (
    SzlabMixerRobotDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.szlab_mixer.stirrer import (
    SzlabMixerStirrerDevice,
)

__all__ = [
    "SzlabMixerPhotoShottingDevice",
    "SzlabMixerPumpDevice",
    "SzlabMixerRobotDevice",
    "SzlabMixerStirrerDevice",
]
