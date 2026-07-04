"""
Guessed driver for Agilent BioTek 406 FX Washer Dispenser.
Generated from deep-search evidence; not tested against real hardware.
"""
from unilabos.registry.decorators import action, device


@device(
    id="agilent_biotek_406_fx",
    category=["plate_washer_dispenser"],
    description="一体式微孔板洗涤与批量加液工作站，可用于细胞板、ELISA板等微孔板的洗涤、缓冲液或试剂分配，以及自动化流程中的方法调用与运行状态管理。",
    model={
        "type": "device",
        "mesh": "agilent_biotek_406_fx",
        "path": "https://uni-lab.oss-cn-zhangjiakou.aliyuncs.com/uni-lab/devices/agilent_biotek_406_fx/macro_device.xacro",
    },
)
class AgilentBioTek406FX:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.device_id = device_id or "agilent_biotek_406_fx"
        self.config = config or {}

    @action()
    def prime_fluidics(self, manifold: str = "dual_action", reagent: str = "wash_buffer") -> dict:
        """对洗板/分液流路进行预充液或预冲洗，为后续运行建立稳定液路状态。"""
        print(f"[{self.device_id}] Priming manifold={manifold} with reagent='{reagent}' for the next wash/dispense cycle")
        return {"success": True}

    @action()
    def wash_plate(self, plate_id: str, protocol_name: str = "cell_binding_wash", cycle_count: int = 3) -> dict:
        """按指定方法对微孔板执行自动洗涤流程。"""
        print(f"[{self.device_id}] Washing plate {plate_id} with protocol '{protocol_name}' for {cycle_count} cycles")
        return {"success": True}

    @action()
    def dispense_bulk_reagent(self, plate_id: str, reagent: str, volume_ul: float) -> dict:
        """向目标微孔板批量分配缓冲液、培养基或其他试剂。"""
        print(f"[{self.device_id}] Dispensing reagent '{reagent}' to plate {plate_id} at volume={volume_ul} uL per well")
        return {"success": True}

    @action()
    def get_status(self) -> dict:
        """查询设备就绪状态、当前模块状态和任务执行状态。"""
        print(f"[{self.device_id}] Reading washer/dispenser status, manifold readiness, and current run state")
        return {"success": True}
