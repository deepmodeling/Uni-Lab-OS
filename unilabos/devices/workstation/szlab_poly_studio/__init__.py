from typing import TYPE_CHECKING

__all__ = [
    "SZLabPolyPLCDevice",
    "SZLabPolyStudioDeck",
    "S1Workstation",
    "SzlabMixerPumpDevice",
    "SzlabMixerPhotoShottingDevice",
    "SzlabMixerMagneticStirrerDevice",
    "SzlabMixerPipettingStationDevice",
    "powder_container_placeholder_warehouse",
    "s1_loading_buffer_warehouse",
    "s2_tip_placeholder_warehouse",
    "s3_unused_beaker_warehouse",
    "s3_unused_sample_vial_warehouse",
    "s10_liquid_reagent_placeholder_warehouse",
    "s11_used_beaker_warehouse",
    "s11_used_sample_vial_warehouse",
]

if TYPE_CHECKING:
    from unilabos.devices.workstation.szlab_poly_studio.decks import SZLabPolyStudioDeck
    from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.magnetic_stirring import SzlabMixerMagneticStirrerDevice
    from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting import SzlabMixerPhotoShottingDevice
    from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
    from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice
    from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station import (
        SzlabMixerPipettingStationDevice,
    )
    from unilabos.devices.workstation.szlab_poly_studio.s1_synthesis import S1Workstation
    from unilabos.devices.workstation.szlab_poly_studio.warehouses import (
        powder_container_placeholder_warehouse,
        s1_loading_buffer_warehouse,
        s2_tip_placeholder_warehouse,
        s3_unused_beaker_warehouse,
        s3_unused_sample_vial_warehouse,
        s10_liquid_reagent_placeholder_warehouse,
        s11_used_beaker_warehouse,
        s11_used_sample_vial_warehouse,
    )


def __getattr__(name: str):
    if name == "SZLabPolyPLCDevice":
        from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

        return SZLabPolyPLCDevice
    if name == "SZLabPolyStudioDeck":
        from unilabos.devices.workstation.szlab_poly_studio.decks import SZLabPolyStudioDeck

        return SZLabPolyStudioDeck
    if name == "S1Workstation":
        from unilabos.devices.workstation.szlab_poly_studio.s1_synthesis import S1Workstation

        return S1Workstation
    if name == "SzlabMixerPumpDevice":
        from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice

        return SzlabMixerPumpDevice
    if name == "SzlabMixerPhotoShottingDevice":
        from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting import SzlabMixerPhotoShottingDevice

        return SzlabMixerPhotoShottingDevice
    if name == "SzlabMixerMagneticStirrerDevice":
        from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.magnetic_stirring import (
            SzlabMixerMagneticStirrerDevice,
        )

        return SzlabMixerMagneticStirrerDevice
    if name == "SzlabMixerPipettingStationDevice":
        from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station import (
            SzlabMixerPipettingStationDevice,
        )

        return SzlabMixerPipettingStationDevice
    if name in {
        "powder_container_placeholder_warehouse",
        "s1_loading_buffer_warehouse",
        "s2_tip_placeholder_warehouse",
        "s3_unused_beaker_warehouse",
        "s3_unused_sample_vial_warehouse",
        "s10_liquid_reagent_placeholder_warehouse",
        "s11_used_beaker_warehouse",
        "s11_used_sample_vial_warehouse",
    }:
        from unilabos.devices.workstation.szlab_poly_studio import warehouses

        return getattr(warehouses, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
