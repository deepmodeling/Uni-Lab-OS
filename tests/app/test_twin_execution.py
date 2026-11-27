"""物理结果和库存回执的双门禁，不以本地过程封存代替库存。"""
import pytest

from unilabos.simulation.execution import DeviceWorldExecutor


class World:
    tick = 0
    def scan(self):
        self.tick += 1
        return {"tick": self.tick}


class Model:
    def __init__(self): self.events=[]
    def command(self,*args): self.events.append("command")
    def accept_state(self,state): self.events.append(state["tick"])
    def completion(self): return {"physical_tick":1}
    def reset_command(self): self.events.append("reset")


def test_receipt_failure_prevents_success_and_reexecution():
    world, model = World(), Model()
    records=[]
    executor=DeviceWorldExecutor(world,model,settle=lambda *args: {},authorize=lambda request:None,record=records.append)
    request={"identity":{"node_job_uuid":"j","task_uuid":"t"},"operation":"move","parameters":{}}
    with pytest.raises(RuntimeError,match="canonical_inventory_receipt_missing"):executor.execute(request)
    assert model.events==["command",1,"reset",2]
    assert all(record["event"]!="action.completed" for record in records)
    with pytest.raises(RuntimeError,match="world_action_unknown"):executor.execute(request)
    assert world.tick==2


def test_duplicate_success_does_not_redo_motion_or_inventory():
    world,model=World(),Model();settled=[]
    def settle(*args):
        settled.append(args)
        return {"receipt_id":"ledger-1"}
    executor=DeviceWorldExecutor(world,model,settle=settle,authorize=lambda request:None,record=lambda event:None)
    request={"identity":{"node_job_uuid":"j","task_uuid":"t"},"operation":"move","parameters":{}}
    assert executor.execute(request)==executor.execute(request)
    assert world.tick==2 and len(settled)==1
    with pytest.raises(ValueError,match="action_replay_conflict"):executor.execute({**request,"parameters":{"x":2}})


def test_action_wire_uses_canonical_resource_reference():
    import json

    from pylabrobot.resources import Resource

    from unilabos.simulation.action_client import _resource_reference
    from unilabos.utils.type_check import UNILABOS_REFERENCE_UUID_ATTR
    resource=Resource("local-name",1,1,1)
    resource.unilabos_uuid="edge-private"
    setattr(resource,UNILABOS_REFERENCE_UUID_ATTR,"canonical-material")
    assert json.loads(json.dumps({"resource":resource},default=_resource_reference))=={"resource":{"uuid":"canonical-material"}}
    with pytest.raises(TypeError,match="native_resource_reference_missing"):
        json.dumps(Resource("unbound",1,1,1),default=_resource_reference)


def test_observer_receives_confirmed_and_reset_scans_before_receipt():
    world,model=World(),Model();events=[]
    def settle(*args):
        assert events==[1,2]
        return {'receipt_id':'ledger'}
    executor=DeviceWorldExecutor(world,model,settle=settle,authorize=lambda request:None,
        record=lambda event:None,observe=lambda state,request:events.append(state['tick']))
    request={'identity':{'node_job_uuid':'j','task_uuid':'t'},'operation':'move','parameters':{}}
    executor.execute(request)
    assert events==[1,2]
