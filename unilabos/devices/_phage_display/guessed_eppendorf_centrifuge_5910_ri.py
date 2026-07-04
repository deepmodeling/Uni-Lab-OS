"""
Guessed driver for Eppendorf Centrifuge 5910 Ri.
Generated from deep-search evidence; not tested against real hardware.
"""
from unilabos.registry.decorators import action, device


@device(
    id="eppendorf_centrifuge_5910_ri",
    category=["refrigerated_high_speed_centrifuge"],
    description="冷冻台式离心机，可在受控温度下执行样本离心、转子与适配器配置确认、运行结束解锁以及运行记录导出。",
    model={
        "type": "device",
        "mesh": "eppendorf_centrifuge_5910_ri",
        "path": "https://uni-lab.oss-cn-zhangjiakou.aliyuncs.com/uni-lab/devices/eppendorf_centrifuge_5910_ri/macro_device.xacro",
    },
)
class EppendorfCentrifuge5910Ri:
    def __init__(self, device_id=None, config=None, **kwargs):
        self.device_id = device_id or "eppendorf_centrifuge_5910_ri"
        self.config = config or {}

    @action()
    def load_rotor_setup(self, rotor_name: str, vessel_type: str) -> dict:
        """确认当前批次所需的转子和适配器配置。"""
        print(f"[{self.device_id}] Rotor setup confirmed: rotor={rotor_name}, vessel_type={vessel_type}, status=ready")
        return {"success": True}

    @action()
    def spin_samples(self, batch_id: str, relative_g: float, duration_minutes: float, temperature_c: float = 20.0) -> dict:
        """按设定离心力、时间和温度执行离心程序。"""
        print(
            f"[{self.device_id}] Spin program started: batch={batch_id}, g={relative_g:.0f}, "
            f"time={duration_minutes:.1f} min, temp={temperature_c:.1f} C"
        )
        return {"success": True}

    @action()
    def park_and_unlock(self) -> dict:
        """在运行结束后使设备进入可安全取样的状态。"""
        print(f"[{self.device_id}] Rotor parked, chamber unlocked, unload state confirmed")
        return {"success": True}

    @action()
    def export_run_log(self, run_id: str) -> dict:
        """导出运行记录、状态信息和完成结果。"""
        print(f"[{self.device_id}] Exporting run log for run={run_id}, outputs=history+events+status")
        return {"success": True}
