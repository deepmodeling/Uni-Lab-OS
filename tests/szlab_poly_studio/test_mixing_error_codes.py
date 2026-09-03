import pytest

from unilabos.devices.workstation.szlab_poly_studio.error_codes import (
    MIXING_ERROR_CATALOG,
    PLC_ALARM_MAP,
    enrich_mixing_failure,
    enrich_with_plc_alarm,
    plc_alarm_for_address,
    read_active_plc_alarms,
)
from unilabos.devices.workstation.szlab_poly_studio.sensor import (
    wait_sensor_conditions,
    wait_variable_equal,
)


def test_catalog_contains_documented_stations():
    assert {item.station for item in MIXING_ERROR_CATALOG.values()} == {
        "S04", "S05", "S06", "S07", "S08", "S09", "S12"
    }


def test_enriches_s06_not_ready_failure():
    result = enrich_mixing_failure(
        {"success": False, "message": "等待 S06 准备信号失败"},
        device_id="szlab_mixer_pump",
    )

    assert result is not None
    assert result["error_code"] == "MIX-S06-001"
    assert result["station"] == "S06"
    assert result["recovery"]
    assert result["message"].startswith("[MIX-S06-001]")


def test_selects_specific_s09_tip_liquid_code():
    result = enrich_mixing_failure(
        {"success": False, "message": "S09 TIP 吸液时触底"},
        device_id="szlab_pipetting_station",
    )

    assert result is not None
    assert result["error_code"] == "MIX-S09-003"


def test_unrelated_device_failure_is_not_relabelled():
    assert enrich_mixing_failure(
        {"success": False, "message": "network failed"},
        device_id="other_device",
    ) is None


def test_csv_plc_addresses_have_deterministic_codes():
    assert len(PLC_ALARM_MAP) == 29
    assert plc_alarm_for_address("D500.0").code == "MIX-COMMON-101"
    assert plc_alarm_for_address("d510.0").code == "MIX-S05-101"
    assert plc_alarm_for_address("D518.5").code == "MIX-S09-106"


def test_reads_active_alarm_from_symbolic_bit():
    class Reader:
        def read_variable(self, name, use_cache=False):
            del use_cache
            return name == "S09报警[0].NO[5]"

    alarms = read_active_plc_alarms(Reader(), stations={"S09"})

    assert [alarm.address for alarm in alarms] == ["D518.5"]


def test_reads_alarm_from_parent_word_when_scalar_bit_is_stale_false():
    class Reader:
        def read_variable(self, name, use_cache=False):
            del use_cache
            if name == "S09报警[0].NO[2]":
                return False
            if name == "S09报警[0]":
                return 1 << 2
            raise KeyError(name)

    alarms = read_active_plc_alarms(Reader(), stations={"S09"})

    assert [alarm.address for alarm in alarms] == ["D518.2"]
    assert alarms[0].code == "MIX-S09-103"


@pytest.mark.parametrize(
    ("station", "scalar_name", "parent_name", "bit", "address", "code"),
    [
        ("公共", "公共_报警[0].NO[4]", "公共_报警[0]", 4, "D500.4", "MIX-COMMON-105"),
        ("S05", "S05报警[0].NO[1]", "S05报警[0]", 1, "D510.1", "MIX-S05-102"),
        ("S07", "S07报警[0].NO[0]", "S07报警[0]", 0, "D514.0", "MIX-S07-101"),
        ("S08", "S08报警[0].NO[2]", "S08报警[0]", 2, "D516.2", "MIX-S08-103"),
        ("S10", "S10报警[0].NO[1]", "S10报警[0]", 1, "D520.1", "MIX-S10-102"),
        ("S11", "S11报警[0].NO[0]", "S11报警[0]", 0, "D522.0", "MIX-S11-101"),
        ("S12", "S12报警[0].NO[3]", "S12报警[0]", 3, "D524.3", "MIX-S12-104"),
    ],
)
def test_all_alarm_groups_fall_back_to_parent_word(
    station, scalar_name, parent_name, bit, address, code
):
    class Reader:
        def read_variable(self, name, use_cache=False):
            del use_cache
            if name == scalar_name:
                return False
            if name == parent_name:
                return 1 << bit
            raise KeyError(name)

    alarms = read_active_plc_alarms(Reader(), stations={station})

    assert [(alarm.address, alarm.code) for alarm in alarms] == [(address, code)]


def test_alarm_falls_back_to_root_word_array():
    class Reader:
        def read_variable(self, name, use_cache=False):
            del use_cache
            if name in {"S08报警[0].NO[1]", "S08报警[0]"}:
                return False
            if name == "S08报警":
                return [1 << 1, 0]
            raise KeyError(name)

    alarms = read_active_plc_alarms(Reader(), stations={"S08"})

    assert [(alarm.address, alarm.code) for alarm in alarms] == [
        ("D516.1", "MIX-S08-102")
    ]


def test_variable_wait_stops_when_plc_alarm_appears():
    class Reader:
        def __init__(self):
            self.abort_checks = 0

        def read_variable(self, name, use_cache=False):
            del name, use_cache
            return False

        def _mixing_wait_should_abort(self):
            self.abort_checks += 1
            return True

    reader = Reader()

    assert wait_variable_equal(reader, "S09工艺完成", 5, interval=0) is False
    assert reader.abort_checks == 1


def test_sensor_wait_stops_when_plc_alarm_appears():
    class Reader:
        def read_variable(self, name, use_cache=False):
            del name, use_cache
            return False

        def _mixing_wait_should_abort(self):
            return True

    success, values = wait_sensor_conditions(
        Reader(),
        {"S09TIP盒检测1": True},
        interval=0,
    )

    assert success is False
    assert values == {"S09TIP盒检测1": False}


def test_sensor_wait_returns_false_on_timeout():
    class Reader:
        def read_variable(self, name, use_cache=False):
            del name, use_cache
            return False

    success, values = wait_sensor_conditions(
        Reader(),
        {"S05准备信号": True},
        interval=0,
        timeout=0.001,
    )

    assert success is False
    assert values == {"S05准备信号": False}




def test_falls_back_to_raw_d_word_and_enriches_failure():
    class Reader:
        def read_variable(self, name, use_cache=False):
            del use_cache
            if name == "D510":
                return 1
            raise KeyError(name)

    alarm = read_active_plc_alarms(Reader(), stations={"S05"})[0]
    result = enrich_with_plc_alarm(
        {"success": False, "message": "拍照动作失败"},
        alarm=alarm,
    )

    assert result["code"] == "MIX-S05-101"
    assert result["plc_address"] == "D510.0"
    assert "D510.0=ON" in result["message"]
