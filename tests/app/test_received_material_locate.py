"""正式收货和本站历史输入沿用中心准入，位置读取不能借用其他任务授权。"""

from types import SimpleNamespace

import pytest

from tests.app.test_local_material_inputs import local_package
from tests.app.test_station_material_inputs import received  # noqa: F401
from unilabos.app.scheduler.inventory.domain import CommandRejected
from unilabos.app.scheduler.inventory.material_usage import locate_material, mark_material_used


@pytest.mark.parametrize("historical", [False, True], ids=["received", "local_history"])
def test_locate_accepts_only_current_tasks_active_center_material_input(received, historical):
    s = received
    body = local_package(s) if historical else s["body"]
    authority = SimpleNamespace(store=s["stores"]["target"])
    material = {"uuid": s["local"]}
    with pytest.raises(CommandRejected, match="准入"):
        locate_material(authority, task_uuid="unadmitted", material=material)
    receipt = s["authority"].prepare(body, input_materials=s["inputs"])
    task = receipt["local_task_uuid"]
    before = list(authority.store._conn.iterdump())
    result = locate_material(authority, task_uuid=task, material=material)
    assert result["material"] == material
    assert result["site_uuid"] == s["sites"]["target"]
    assert list(authority.store._conn.iterdump()) == before
    with pytest.raises(CommandRejected, match="准入"):
        locate_material(authority, task_uuid="another-task", material=material)
    # 只扩展读取权限，不能把中心输入占用当成来源的独占写权限。
    with pytest.raises(CommandRejected, match="独占"):
        mark_material_used(authority, task_uuid=task, job_uuid="ungranted-write", material=material)
    s["authority"].release(body, receipt)
    with pytest.raises(CommandRejected, match="准入"):
        locate_material(authority, task_uuid=task, material=material)
