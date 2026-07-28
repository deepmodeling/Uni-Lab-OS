from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.sensors import (
    s04_material_sensor_var,
)
from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.sensors import (
    S05_MATERIAL_SENSOR,
)
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import (
    ADDITION_BEAKER_SENSOR,
)
from unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station import (
    CAP_STORAGE_SLOT_SENSORS,
    SENSOR_CAP_STATION,
)
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_STATION_SENSORS,
    S09_TIP_BOX_SENSORS,
)
from unilabos.devices.workstation.szlab_poly_studio.sensor import (
    STACK_SENSOR_GROUPS,
    S02Sensors,
    S03Sensors,
    S04Sensors,
    S05Sensors,
    S06Sensors,
    S07Sensors,
    S08Sensors,
    S09Sensors,
    S10Sensors,
    S11Sensors,
    SensorBase,
    wait_sensor_conditions,
)


def test_station_sensor_definitions_inherit_common_base() -> None:
    sensor_classes = (
        S02Sensors,
        S03Sensors,
        S04Sensors,
        S05Sensors,
        S06Sensors,
        S07Sensors,
        S08Sensors,
        S09Sensors,
        S10Sensors,
        S11Sensors,
    )

    assert all(issubclass(sensor_class, SensorBase) for sensor_class in sensor_classes)


def test_sensor_bit_name_helpers_validate_and_parse() -> None:
    variable_name = SensorBase.bit(3, 14)

    assert variable_name == "传感器状态_上位机[3].NO[14]"
    assert SensorBase.array(3) == "传感器状态_上位机[3].NO"
    assert SensorBase.parse_bit_name(variable_name) == (3, 14)
    assert SensorBase.parse_bit_name("S08允许加工") is None


def test_legacy_station_sensor_exports_reference_unified_definitions() -> None:
    assert s04_material_sensor_var(1) == S04Sensors.MATERIAL_BY_POSITION[1]
    assert S05_MATERIAL_SENSOR == S05Sensors.MATERIAL
    assert ADDITION_BEAKER_SENSOR == S06Sensors.MATERIAL
    assert SENSOR_CAP_STATION == S08Sensors.CAP_STATION
    assert CAP_STORAGE_SLOT_SENSORS == S08Sensors.CAP_STORAGE_SLOT
    assert S09_TIP_BOX_SENSORS == S09Sensors.TIP_BOX
    assert S09_STATION_SENSORS == S09Sensors.STATION


def test_stack_sensor_classes_use_central_layout() -> None:
    assert S02Sensors.TIP_BOX == STACK_SENSOR_GROUPS["s2_tip"]
    assert S03Sensors.UNUSED_BEAKER == STACK_SENSOR_GROUPS["s3_unused_beaker"]
    assert S07Sensors.POWDER_CONTAINER_BY_POSITION == STACK_SENSOR_GROUPS["powder_container"]
    assert S10Sensors.LIQUID_REAGENT == STACK_SENSOR_GROUPS["s10_liquid_reagent"]
    assert S11Sensors.USED_SAMPLE_VIAL == STACK_SENSOR_GROUPS["s11_used_sample_vial"]


def test_common_sensor_wait_method_reads_reader() -> None:
    class Reader:
        def read_variable(self, variable_name: str, use_cache: bool = False) -> bool:
            assert variable_name == S05Sensors.MATERIAL
            assert use_cache is False
            return True

    success, values = wait_sensor_conditions(
        Reader(),
        {S05Sensors.MATERIAL: True},
        interval=0.0,
    )

    assert success is True
    assert values == {S05Sensors.MATERIAL: True}
