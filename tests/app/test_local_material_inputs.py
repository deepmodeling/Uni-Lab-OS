"""本站历史输入复用已有谱系，不虚构新的跨站交接。"""
from copy import deepcopy

import pytest

from tests.app.test_station_material_inputs import received  # noqa: F401
from unilabos.app.scheduler.inventory.material_inputs import MaterialInputError, StationMaterialInputAuthority
from unilabos.center_protocol.digest import digest


def local_package(s):
    body = deepcopy(s["body"])
    body["payload"]["handoff_uuids"] = []
    for reference in body["payload"]["material_refs"]:
        reference["handoff_item_uuid"] = None
    body["payload_digest"] = digest(body["payload"])
    return body


def test_local_history_input_reuses_verified_inventory_and_stable_task(received):
    s = received
    body = local_package(s)
    authority = StationMaterialInputAuthority(s["auth"]["target"])
    receipt = authority.prepare(body, input_materials=s["inputs"])
    assert authority.prepare(body, input_materials=s["inputs"]) == receipt
    authority.verify(body, receipt, require_current=True)
    assert s["stores"]["target"].query_one("SELECT count(*) n FROM station_material_input")["n"] == 1


@pytest.mark.parametrize("fault", ["lineage", "revision", "template", "foreign_station", "active_handoff"])
def test_local_history_cannot_invent_or_change_frozen_authority(received, fault):
    s = received
    body = local_package(s)
    from uuid import uuid4
    if fault == "lineage":
        body["payload"]["material_refs"][0]["material_lineage_uuid"] = str(uuid4())
    elif fault == "revision":
        body["payload"]["material_refs"][0]["local_revision"] += 1
    elif fault == "template":
        s["inputs"][0]["resource_template_uuid"] = str(uuid4())
    elif fault == "foreign_station":
        body["payload"]["material_refs"][0]["station_uuid"] = str(uuid4())
    else:
        with s["stores"]["target"].transaction() as c:
            c.execute("UPDATE material SET inventory_status='handoff_quarantined' WHERE uuid=?", (s["local"],))
    body["payload_digest"] = digest(body["payload"])
    with pytest.raises(MaterialInputError):
        s["authority"].prepare(body, input_materials=s["inputs"])
    assert s["stores"]["target"].query_one("SELECT count(*) n FROM station_material_input")["n"] == 0


def test_local_history_new_task_uses_real_input_receipt_without_fabricated_handoff(tmp_path, received, monkeypatch):
    from uuid import uuid4

    from tests.app.center_client.test_material_inputs import material_runtime
    from unilabos.app.center_client.client import TaskPackage
    from unilabos.app.center_client.runtime import submission
    s=received
    s['body']=local_package(s)
    s['body']['payload']['global_task_uuid']=str(uuid4())
    s['body']['payload_digest']=digest(s['body']['payload'])
    with material_runtime(tmp_path,s,monkeypatch) as (runtime,center,service,store,durable):
        runtime.poll_once()
        task=service.find_station_workflow_submission(**submission(TaskPackage.from_response(s['body'])))
        assert task['status']=='succeeded'
        proof=durable.material_input(s['body']['payload']['station_invocation_uuid'])
        assert proof['receipt']['local_task_uuid']==task['uuid']
        assert proof['state']=='released'
        assert s['body']['payload']['handoff_uuids']==[]
        assert all(ref['handoff_item_uuid'] is None for ref in s['body']['payload']['material_refs'])
