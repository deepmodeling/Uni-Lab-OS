"""S08 测试共用 fixture。"""

from __future__ import annotations

from tests.szlab_poly_studio.pseudo_clients.s08_decap import (
    PseudoSzlabS08OpcUaClient,
)
from unilabos.devices.workstation.szlab_poly_studio.s08_decap import decap_s08_cap_station as s08_module

NODE_PARAMS_WRITTEN = s08_module.NODE_PARAMS_WRITTEN
NODE_PROCESS_SELECT = s08_module.NODE_PROCESS_SELECT
NODE_PROCESS_COMPLETE = s08_module.NODE_PROCESS_COMPLETE
NODE_STATION_STATUS = s08_module.NODE_STATION_STATUS
S08ProcessType = s08_module.S08ProcessType
SZLabS08CapStationDevice = s08_module.SZLabS08CapStationDevice
_cap_cache_element_name = s08_module._cap_cache_element_name

# 测试模块内沿用旧别名，便于断言驱动模块级函数
s08_module = s08_module

SAMPLE_A = [101, 102, 103]
SAMPLE_B = [201, 202, 203]


def make_s08_device(
    client: PseudoSzlabS08OpcUaClient | None = None,
    *,
    require_station_status: bool = False,
    validate_cap_constraints: bool = False,
) -> tuple[SZLabS08CapStationDevice, PseudoSzlabS08OpcUaClient]:
    pseudo = client or PseudoSzlabS08OpcUaClient()
    device = SZLabS08CapStationDevice(
        url="opc.tcp://127.0.0.1:0/unused",
        opcua_client=pseudo,
    )
    if require_station_status:
        device.require_station_status = True
    if validate_cap_constraints:
        device.validate_cap_constraints = True
    return device, pseudo
