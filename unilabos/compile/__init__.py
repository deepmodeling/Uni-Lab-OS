from unilabos.messages import *
from .pump_protocol import generate_pump_protocol, generate_pump_protocol_with_rinsing
from .clean_protocol import generate_clean_protocol
from .separate_protocol import generate_separate_protocol
from .evaporate_protocol import generate_evaporate_protocol
from .evacuateandrefill_protocol import generate_evacuateandrefill_protocol
from .agv_transfer_protocol import generate_agv_transfer_protocol
from .batch_transfer_protocol import generate_batch_transfer_protocol
from .add_protocol import generate_add_protocol
from .centrifuge_protocol import generate_centrifuge_protocol
from .filter_protocol import generate_filter_protocol
from .heatchill_protocol import (
    generate_heat_chill_protocol, 
    generate_heat_chill_start_protocol, 
    generate_heat_chill_stop_protocol,
    generate_heat_chill_to_temp_protocol  # 保留导入，但不注册为协议
)
from .stir_protocol import generate_stir_protocol, generate_start_stir_protocol, generate_stop_stir_protocol
from .clean_vessel_protocol import generate_clean_vessel_protocol
from .dissolve_protocol import generate_dissolve_protocol
from .filter_through_protocol import generate_filter_through_protocol
from .run_column_protocol import generate_run_column_protocol
from .wash_solid_protocol import generate_wash_solid_protocol
from .adjustph_protocol import generate_adjust_ph_protocol
from .reset_handling_protocol import generate_reset_handling_protocol
from .dry_protocol import generate_dry_protocol
from .recrystallize_protocol import generate_recrystallize_protocol
from .hydrogenate_protocol import generate_hydrogenate_protocol


# Define a dictionary of protocol generators.
action_protocol_generators = {
    AddProtocol: generate_add_protocol,
    AGVTransferProtocol: generate_agv_transfer_protocol,
    BatchTransferProtocol: generate_batch_transfer_protocol,
    AdjustPHProtocol: generate_adjust_ph_protocol,
    CentrifugeProtocol: generate_centrifuge_protocol,
    CleanProtocol: generate_clean_protocol,
    CleanVesselProtocol: generate_clean_vessel_protocol,
    DissolveProtocol: generate_dissolve_protocol,
    DryProtocol: generate_dry_protocol,
    EvacuateAndRefillProtocol: generate_evacuateandrefill_protocol,
    EvaporateProtocol: generate_evaporate_protocol,
    FilterProtocol: generate_filter_protocol,
    FilterThroughProtocol: generate_filter_through_protocol,
    HeatChillProtocol: generate_heat_chill_protocol,
    HeatChillStartProtocol: generate_heat_chill_start_protocol,
    HeatChillStopProtocol: generate_heat_chill_stop_protocol,
    HydrogenateProtocol: generate_hydrogenate_protocol,
    PumpTransferProtocol: generate_pump_protocol_with_rinsing,
    TransferProtocol: generate_pump_protocol,
    RecrystallizeProtocol: generate_recrystallize_protocol,
    ResetHandlingProtocol: generate_reset_handling_protocol,
    RunColumnProtocol: generate_run_column_protocol,
    SeparateProtocol: generate_separate_protocol,
    StartStirProtocol: generate_start_stir_protocol,
    StirProtocol: generate_stir_protocol,
    StopStirProtocol: generate_stop_stir_protocol,
    WashSolidProtocol: generate_wash_solid_protocol,
}