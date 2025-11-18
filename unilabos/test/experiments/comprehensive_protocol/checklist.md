1. 用到的仪器
                virtual_multiway_valve(√)                                                     八通阀门
                virtual_transfer_pump(√)                                                        转移泵
                virtual_centrifuge()                                                            离心机
                virtual_rotavap()                                                               旋蒸仪
                virtual_heatchill()                                                             加热器
                virtual_stirrer()                                                               搅拌器
                virtual_solenoid_valve()                                                        电磁阀
                virtual_vacuum_pump(√)                       vacuum_pump.mock                            真空泵
                virtual_gas_source(√)                                                                    气源
                virtual_filter()                                                                过滤器
                virtual_column(√)                                                               层析柱
                separator()                         homemade_grbl_conductivity                  分液漏斗
2. 用到的protocol
    PumpTransferProtocol: generate_pump_protocol_with_rinsing,                      (√)
    这个重复了，删掉CleanProtocol: generate_clean_protocol,
    SeparateProtocol: generate_separate_protocol,                           (×)
    EvaporateProtocol: generate_evaporate_protocol,                                 (√)
    EvacuateAndRefillProtocol: generate_evacuateandrefill_protocol,                 (√)
    CentrifugeProtocol: generate_centrifuge_protocol,                               (√)
    AddProtocol: generate_add_protocol,                                             (√)
    FilterProtocol: generate_filter_protocol,                                       (√)
    HeatChillProtocol: generate_heat_chill_protocol,                                (√)
    HeatChillStartProtocol: generate_heat_chill_start_protocol,                     (√)
    HeatChillStopProtocol: generate_heat_chill_stop_protocol,                       (√)
    HeatChillToTempProtocol:
    StirProtocol: generate_stir_protocol,                                           (√)
    StartStirProtocol: generate_start_stir_protocol,                                (√)
    StopStirProtocol: generate_stop_stir_protocol,                                  (√)
    这个重复了，删掉TransferProtocol: generate_transfer_protocol,
    CleanVesselProtocol: generate_clean_vessel_protocol,                            (√)
    DissolveProtocol: generate_dissolve_protocol,                                   (√)
    FilterThroughProtocol: generate_filter_through_protocol,                        (√)
    RunColumnProtocol: generate_run_column_protocol,                                (√)<RunColumn Rf="?" column="column" from_vessel="rotavap" ratio="5:95" solvent1="methanol" solvent2="chloroform" to_vessel="rotavap"/>

上下文体积搜索
3. 还没创建的protocol
    ResetHandling                         写完了                               <ResetHandling solvent="methanol"/>
    Dry                                  写完了                                 <Dry compound="product" vessel="filter"/>
    AdjustPH                               写完了                                <AdjustPH pH="8.0" reagent="hydrochloric acid" vessel="main_reactor"/>
    Recrystallize                          写完了                              <Recrystallize ratio="?" solvent1="dichloromethane" solvent2="methanol" vessel="filter" volume="?"/>
    TakeSample                                                                  <TakeSample id="a" vessel="rotavap"/>
    Hydrogenate                                                                 <Hydrogenate temp="45 °C" time="?" vessel="main_reactor"/>
4. 参数对齐










class PumpTransferProtocol(BaseModel):
    from_vessel: str
    to_vessel: str
    volume: float
    amount: str = ""
    time: float = 0
    viscous: bool = False
    rinsing_solvent: str = "air"        <Transfer from_vessel="main_reactor" to_vessel="rotavap"/>
    rinsing_volume: float = 5000        <Transfer event="A" from_vessel="reactor" rate_spec="dropwise" to_vessel="main_reactor"/>
    rinsing_repeats: int = 2            <Transfer from_vessel="separator" through="cartridge" to_vessel="rotavap"/>
    solid: bool = False                 测完了三个都能跑✅
    flowrate: float = 500
    transfer_flowrate: float = 2500

class SeparateProtocol(BaseModel):
    purpose: str
    product_phase: str
    from_vessel: str
    separation_vessel: str
    to_vessel: str
    waste_phase_to_vessel: str
    solvent: str
    solvent_volume: float               <Separate product_phase="bottom" purpose="wash" solvent="water" vessel="separator" volume="?"/>
    through: str                        <Separate product_phase="top" purpose="separate" vessel="separator"/>
    repeats: int                        <Separate product_phase="bottom" purpose="extract" repeats="3" solvent="CH2Cl2" vessel="separator" volume="?"/>
    stir_time: float                    <Separate product_phase="top" product_vessel="flask" purpose="separate" vessel="separator" waste_vessel="separator"/>
    stir_speed: float
    settling_time: float                测完了能跑✅


class EvaporateProtocol(BaseModel):
    vessel: str
    pressure: float
    temp: float                         <Evaporate solvent="ethanol" vessel="rotavap"/>
    time: float                         测完了能跑✅
    stir_speed: float


class EvacuateAndRefillProtocol(BaseModel):
    vessel: str
    gas: str                            <EvacuateAndRefill gas="nitrogen" vessel="main_reactor"/>
    repeats: int                        测完了能跑✅

class AddProtocol(BaseModel):
    vessel: str
    reagent: str
    volume: float
    mass: float
    amount: str
    time: float
    stir: bool
    stir_speed: float                   <Add reagent="ethanol" vessel="main_reactor" volume="2.7 mL"/>
    <Add event="A" mass="19.3 g" mol="0.28 mol" rate_spec="portionwise" reagent="sodium nitrite" time="1 h" vessel="main_reactor"/>
    <Add mass="4.5 g" mol="16.2 mmol" reagent="(S)-2-phthalimido-6-hydroxyhexanoic acid" vessel="main_reactor"/>
    <Add purpose="dilute" reagent="hydrochloric acid" vessel="main_reactor" volume="?"/>
    <Add equiv="1.1" event="B" mol="25.2 mmol" rate_spec="dropwise" reagent="1-fluoro-2-nitrobenzene" time="20 min" 
    vessel="main_reactor" volume="2.67 mL"/>
    <Add ratio="?" reagent="tetrahydrofuran|tert-butanol" vessel="main_reactor" volume="?"/>
    viscous: bool
    purpose: str                        测完了能跑✅

class CentrifugeProtocol(BaseModel):
    vessel: str
    speed: float
    time: float                         没毛病
    temp: float

class FilterProtocol(BaseModel):
    vessel: str
    filtrate_vessel: str
    stir: bool                          <Filter vessel="filter"/>
    stir_speed: float                   <Filter filtrate_vessel="rotavap" vessel="filter"/>
    temp: float                         测完了能跑✅
    continue_heatchill: bool
    volume: float

class HeatChillProtocol(BaseModel):
    vessel: str
    temp: float
    time: float                         <HeatChill pressure="1 mbar" temp_spec="room temperature" time="?" vessel="main_reactor"/>
                                        <HeatChill temp_spec="room temperature" time_spec="overnight" vessel="main_reactor"/>
                                        <HeatChill temp="256 °C" time="?" vessel="main_reactor"/>
                                        <HeatChill reflux_solvent="methanol" temp_spec="reflux" time="2 h" vessel="main_reactor"/>
                                        <HeatChillToTemp temp_spec="room temperature" vessel="main_reactor"/>
    stir: bool                          测完了能跑✅
    stir_speed: float
    purpose: str

class HeatChillStartProtocol(BaseModel):
    vessel: str
    temp: float                     疑似没有
    purpose: str

class HeatChillStopProtocol(BaseModel):
    vessel: str                     疑似没有

class StirProtocol(BaseModel):
    stir_time: float
    stir_speed: float               <Stir time="0.5 h" vessel="main_reactor"/>
                                    <Stir event="A" time="30 min" vessel="main_reactor"/>
                                    <Stir time_spec="several minutes" vessel="filter"/>
    settling_time: float            测完了能跑✅

class StartStirProtocol(BaseModel):
    vessel: str
    stir_speed: float               疑似没有
    purpose: str

class StopStirProtocol(BaseModel):
    vessel: str                     疑似没有

class TransferProtocol(BaseModel):
    from_vessel: str
    to_vessel: str
    volume: float
    amount: str = ""
    time: float = 0
    viscous: bool = False   
    rinsing_solvent: str = ""
    rinsing_volume: float = 0.0
    rinsing_repeats: int = 0
    solid: bool = False             这个protocol早该删掉了

class CleanVesselProtocol(BaseModel):
    vessel: str
    solvent: str
    volume: float
    temp: float
    repeats: int = 1                    <CleanVessel vessel="centrifuge"/>

class DissolveProtocol(BaseModel):
    vessel: str
    solvent: str
    volume: float       <Dissolve mass="2.9 g" mol="0.12 mol" reagent="magnesium" vessel="main_reactor"/>
    amount: str = ""    <Dissolve mass="12.9 g" reagent="4-tert-butylbenzyl bromide" vessel="main_reactor"/>
    temp: float = 25.0  <Dissolve solvent="diisopropyl ether" vessel="rotavap" volume="?"/>
    time: float = 0.0   <Dissolve event="A" mass="?" reagent="pyridinone" vessel="main_reactor"/>
    stir_speed: float = 0.0     测完了能跑✅

class FilterThroughProtocol(BaseModel):
    from_vessel: str
    to_vessel: str
    filter_through: str
    eluting_solvent: str = ""
    eluting_volume: float = 0.0                 疑似没有
    eluting_repeats: int = 0
    residence_time: float = 0.0

class RunColumnProtocol(BaseModel):
    from_vessel: str
    to_vessel: str              <RunColumn Rf="?" column="column" from_vessel="rotavap" pct1="40 %" pct2="50 %" solvent1="ethyl acetate" solvent2="hexane" to_vessel="rotavap"/>
    column: str                 测完了能跑✅

class WashSolidProtocol(BaseModel):
    vessel: str
    solvent: str
    volume: float
    filtrate_vessel: str = ""   <WashSolid repeats="4" solvent="water" vessel="main_reactor" volume="400 mL"/>
    temp: float = 25.0          <WashSolid filtrate_vessel="rotavap" solvent="formic acid" vessel="main_reactor" volume="?"/>
    stir: bool = False          <WashSolid solvent="acetone" vessel="rotavap" volume="5 mL"/>
                                <WashSolid solvent="ethyl alcohol" vessel="main_reactor" volume_spec="small volume"/>
                                <WashSolid filtrate_vessel="rotavap" mass="10 g" solvent="toluene" vessel="separator"/>
                                <WashSolid repeats_spec="several" solvent="water" vessel="main_reactor" volume="?"/>
    stir_speed: float = 0.0     测完了能跑✅
    time: float = 0.0
    repeats: int = 1

class AdjustPHProtocol(BaseModel):
    vessel: str = Field(..., description="目标容器")
    ph_value: float = Field(..., description="目标pH值")  # 改为 ph_value
    reagent: str = Field(..., description="酸碱试剂名称")
    # 移除其他可选参数，使用默认值                                                   <新写的，没问题>

class ResetHandlingProtocol(BaseModel):
    solvent: str = Field(..., description="溶剂名称")                               <新写的，没问题>

class DryProtocol(BaseModel):
    compound: str = Field(..., description="化合物名称")                            <新写的，没问题>
    vessel: str = Field(..., description="目标容器")

class RecrystallizeProtocol(BaseModel):
    ratio: str = Field(..., description="溶剂比例（如 '1:1', '3:7'）")
    solvent1: str = Field(..., description="第一种溶剂名称")                        <新写的，没问题>
    solvent2: str = Field(..., description="第二种溶剂名称")
    vessel: str = Field(..., description="目标容器")
    volume: float = Field(..., description="总体积 (mL)")

class HydrogenateProtocol(BaseModel):
    temp: str = Field(..., description="反应温度（如 '45 °C'）")
    time: str = Field(..., description="反应时间（如 '2 h'）")                          <新写的，没问题>
    vessel: str = Field(..., description="反应容器")

    还差
    <dissolve>
    <separate>
    <CleanVessel vessel="centrifuge"/>


单位修复：
    evaporate
    heatchill
    recrysitallize
    stir
    wash solid