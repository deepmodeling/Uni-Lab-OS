import time
import asyncio
from pydantic import BaseModel

from unilabos.device_runtime.node import DeviceNode


class Point3D(BaseModel):
    x: float
    y: float
    z: float


def d(a: Point3D, b: Point3D) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5


class MockCNCAsync:
    _ros_node: DeviceNode

    def __init__(self):
        self._position: Point3D = Point3D(x=0.0, y=0.0, z=0.0)
        self._status = "Idle"

    def post_create(self, ros_node):
        self._ros_node = ros_node
    
    @property
    def position(self) -> Point3D:
        return self._position
    
    async def get_position(self):
        return self.position
    
    @property
    def status(self) -> str:
        return self._status
    
    async def set_position(self, position: Point3D, velocity: float = 10.0):
        self._status = "Running"
        current_pos = self.position
        
        move_time = d(position, current_pos) / velocity
        for i in range(20):
            self._position.x = current_pos.x + (position.x - current_pos.x) / 20 * (i+1)
            self._position.y = current_pos.y + (position.y - current_pos.y) / 20 * (i+1)
            self._position.z = current_pos.z + (position.z - current_pos.z) / 20 * (i+1)
            await self._ros_node.sleep(move_time / 20)
        self._status = "Idle"
