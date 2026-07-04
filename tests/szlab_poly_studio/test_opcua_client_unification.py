from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import unilabos.devices.workstation.szlab_poly_studio.plc as plc_module
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice
from unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station import (
    SZLabS08CapStationDevice,
)


def test_station_opcua_client_compatibility_modules_are_removed() -> None:
    assert not hasattr(plc_module, "SzlabOpcUaVariableClient")
    root = Path("unilabos/devices/workstation/szlab_poly_studio")
    assert not (root / "s06_pump/opcua_client.py").exists()
    assert not (root / "s07_solid_addition/opcua_client.py").exists()
    assert not (root / "s08_decap/decap_s08_opcua_client.py").exists()


def test_s06_device_uses_szlab_poly_plc_device_directly() -> None:
    with patch.object(SZLabPolyPLCDevice, "__init__", autospec=True, return_value=None) as init:
        SzlabMixerPumpDevice(
            url="opc.tcp://example:4840",
            username="user",
            password="pass",
            opcua_browse_depth=3,
            opcua_browse_limit=99,
            opcua_node_id_map={"S06允许加工": "ns=4;s=S06允许加工"},
            opcua_allow_recursive_browse=True,
        )

    assert init.call_args.kwargs["csv_path"] is False
    assert init.call_args.kwargs["opcua_object_name"] == "VirtualMixer"
    assert init.call_args.kwargs["opcua_browse_depth"] == 3
    assert init.call_args.kwargs["opcua_browse_limit"] == 99
    assert init.call_args.kwargs["node_id_map"] == {"S06允许加工": "ns=4;s=S06允许加工"}
    assert init.call_args.kwargs["opcua_allow_recursive_browse"] is True


def test_s08_device_uses_szlab_poly_plc_device_directly() -> None:
    with patch.object(SZLabPolyPLCDevice, "__init__", autospec=True, return_value=None) as init:
        with patch.object(SZLabPolyPLCDevice, "write", return_value=None):
            SZLabS08CapStationDevice(
                url="opc.tcp://127.0.0.1:50102/",
                username="user",
                password="pass",
                opcua_browse_depth=4,
                opcua_browse_limit=88,
                opcua_node_id_map={"S08允许加工": "ns=4;s=S08允许加工"},
                opcua_allow_recursive_browse=True,
                opcua_object_name="CustomS08",
            )

    assert init.call_args.kwargs["csv_path"] is False
    assert init.call_args.kwargs["opcua_object_name"] == "CustomS08"
    assert init.call_args.kwargs["opcua_browse_depth"] == 4
    assert init.call_args.kwargs["opcua_browse_limit"] == 88
    assert init.call_args.kwargs["node_id_map"] == {"S08允许加工": "ns=4;s=S08允许加工"}
    assert init.call_args.kwargs["opcua_allow_recursive_browse"] is True
