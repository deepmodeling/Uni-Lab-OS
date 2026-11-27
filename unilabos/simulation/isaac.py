"""可选 Isaac CPU 物理后端。"""
from .physics import ConfirmedStepper, ModelControls


class IsaacPhysicsWorld:
    """SDK 延迟导入，生命周期/物理推进公共化，设备目标和完成条件由包处理。"""

    def __init__(self, stage, world_id: str, session_id: str, *, epoch: int = 0):
        import carb
        from omni.physx import get_physx_simulation_interface
        from omni.physx.bindings import _physx
        from pxr import UsdUtils

        carb.settings.get_settings().set_bool(_physx.SETTING_UPDATE_VELOCITIES_TO_USD, True)
        carb.settings.get_settings().set_bool(_physx.SETTING_OUTPUT_VELOCITIES_LOCAL_SPACE, False)
        self.stage = stage
        self.cache = UsdUtils.StageCache.Get()
        self.ident = self.cache.Insert(stage).ToLongInt()
        self.sim = get_physx_simulation_interface()
        self.sim.attach_stage(self.ident)
        self.stepper = ConfirmedStepper(world_id, session_id, epoch=epoch)

    def scan(self, token, *, before_substep=lambda index: None, after_substep=lambda index: None, observe=lambda: {}):
        def advance(index, duration, time):
            before_substep(index)
            self.sim.simulate(duration, time)
            self.sim.fetch_results()
            after_substep(index)

        return self.stepper.step(token, advance, observe)

    def close(self):
        self.sim.detach_stage()
        self.cache.Erase(self.stage)
