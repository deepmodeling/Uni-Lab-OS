import time


class MockGripper:
    def __init__(self):
        self._position: float = 0.0
        self._velocity: float = 2.0
        self._torque: float = 0.0
        self._status = "Idle"

    @property
    def position(self) -> float:
        return self._position

    @property
    def velocity(self) -> float:
        return self._velocity

    @property
    def torque(self) -> float:
        return self._torque

    @property
    def status(self) -> str:
        return self._status

    def push_to(self, position: float, torque: float, velocity: float = 0.0):
        self._status = "Running"
        current_pos = self.position
        if velocity == 0.0:
            velocity = self.velocity

        move_time = abs(position - current_pos) / velocity
        for i in range(20):
            self._position = current_pos + (position - current_pos) / 20 * (i+1)
            self._torque = torque / (20 - i)
            self._velocity = velocity
            time.sleep(move_time / 20)
        self._torque = torque
        self._status = "Idle"

    def edit_id(self, wf_name: str = "gripper_run", params: str = "{}", resource: dict = {"Gripper1": {}}):
        v = list(resource.values())[0]
        v["sample_id"] = "EDITED"
        time.sleep(10)
        return resource
