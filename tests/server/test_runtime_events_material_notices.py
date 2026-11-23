"""/api/v1/events 的物料失效：账本前进就推一条无 id 的 material.changed，不动工作流游标。

前端每个工站只维持这一条事件流（HTTP/1.1 连接数有限），物料失效与工作流、日志通知走同一条。
通知列出这段账本里变了的物料，前端只重读这些；列不准时省略，前端整体重读。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

from unilabos.server.api.runtime import events


class _Request:
    async def is_disconnected(self):
        return False


class _NoLogNotices:
    def subscribe(self, listener):
        return lambda: None


class _Ledger:
    """最小物料权威：账本末尾、账本条目、Site → 所属物料。"""

    def __init__(self, tail: int, rows=(), sites=None):
        self.tail = tail
        self.rows = [SimpleNamespace(sequence=s, aggregate_type=t, aggregate_uuid=u) for s, t, u in rows]
        self.sites = dict(sites or {})

    def latest_ledger_sequence(self) -> int:
        return self.tail

    def changes(self, *, after_sequence: int = 0, limit: int = 100):
        return [row for row in self.rows if row.sequence > after_sequence][:limit]

    def get_site(self, site_uuid: str, *, include_deleted: bool = False):
        owner = self.sites.get(site_uuid)
        return SimpleNamespace(owner_material_uuid=owner) if owner else None


def _quiet(monkeypatch):
    monkeypatch.setattr(events, "log_notices", _NoLogNotices())
    monkeypatch.setattr(events, "shutting_down", lambda: False)


def _workflow(events_window=None):
    workflow = Mock()
    workflow.read_event_window.return_value = events_window or (1, 41, [])
    return workflow


def _first_frame(materials, cursor: int, workflow=None) -> str:
    async def scenario():
        stream = events.runtime_event_stream(
            _Request(), workflow or _workflow(), cursor=41,
            materials=events.MaterialNotices(materials), material_cursor=cursor,
        )
        assert "connected" in await anext(stream)
        frame = await asyncio.wait_for(anext(stream), 2)
        await stream.aclose()
        return frame

    return asyncio.run(scenario())


def _data(frame: str) -> dict:
    return json.loads(frame.split("data: ")[1])


def test_ledger_advance_lists_the_changed_materials_without_an_id(monkeypatch):
    _quiet(monkeypatch)
    ledger = _Ledger(
        7,
        rows=[(5, "material", "old"), (6, "site", "site-1"), (6, "material", "plate"), (7, "lot", "lot-1"), (7, "material", "plate")],
        sites={"site-1": "deck"},
    )
    frame = _first_frame(ledger, cursor=5)
    assert frame.startswith("event: material.changed\n")
    assert "id:" not in frame  # 不占工作流游标
    # 区间 (5, 7]：Site 归到所属台面，批次不进物料图，重复的物料去重
    assert _data(frame) == {"sequence": 7, "material_uuids": ["deck", "plate"]}


def test_template_change_asks_for_a_full_reload(monkeypatch):
    _quiet(monkeypatch)
    ledger = _Ledger(9, rows=[(8, "material", "plate"), (9, "resource_template", "tpl")])
    assert _data(_first_frame(ledger, cursor=7)) == {"sequence": 9}


def test_ledger_reset_asks_for_a_full_reload(monkeypatch):
    _quiet(monkeypatch)
    # 库被重置：末尾比基线小，列不准
    assert _data(_first_frame(_Ledger(2), cursor=40)) == {"sequence": 2}


def test_unchanged_ledger_sends_nothing_before_workflow_events(monkeypatch):
    _quiet(monkeypatch)
    workflow = _workflow((1, 42, [{"id": 42, "event": "workflow.task.changed", "data": {"uuid": "t"}}]))
    frame = _first_frame(_Ledger(7), cursor=7, workflow=workflow)
    assert frame.startswith("id: 42\nevent: workflow.runtime.changed\n")


def test_ledger_read_failure_does_not_break_the_workflow_stream(monkeypatch):
    _quiet(monkeypatch)

    class Broken(_Ledger):
        def latest_ledger_sequence(self) -> int:
            raise RuntimeError("materials.db is locked")

    workflow = _workflow((1, 42, [{"id": 42, "event": "workflow.task.changed", "data": {"uuid": "t"}}]))
    frame = _first_frame(Broken(0), cursor=3, workflow=workflow)
    assert frame.startswith("id: 42\nevent: workflow.runtime.changed\n")


def test_real_authority_names_the_placed_material_and_the_site_owner(tmp_path):
    from uuid import uuid4

    from unilabos.client.materials import LocalMaterialsClient
    from unilabos.protocol.materials import (
        InventoryMutation,
        MaterialIdentityWrite,
        MaterialNodeCreate,
        MaterialPlacement,
        MaterialTreeCreate,
        ResourceTemplateWrite,
    )
    from unilabos.resources.adapters.device_site import apply_device_authority_state
    from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance
    from unilabos.server.services.materials import MaterialsService

    def mutation(operation: str) -> InventoryMutation:
        command = str(uuid4())
        return InventoryMutation(
            command_uuid=command, effect_key=f"{operation}:{command}", operation=operation,
            actor_type="test", actor_uuid="material-notices",
        )

    service = MaterialsService(tmp_path / "materials.db")
    try:
        service.put_template(
            mutation("put_template"), ResourceTemplateWrite(name="sample_tpl", display_name="sample", class_name="Resource")
        )
        device_uuid = str(uuid4())
        device = ResourceDictInstance(ResourceDict.model_validate({
            "id": "notice-device", "uuid": device_uuid, "name": "notice-device", "type": "device",
            "class": "notice_device", "template_name": "notice_device", "config": {}, "data": {}, "extra": {},
            "sites": None, "sites_initialized": False,
        }))
        apply_device_authority_state(
            device, {"available_sites": [{"index": 1, "label": "s1"}]}, "notice_device",
            gateway=LocalMaterialsClient(service),
        )
        [site] = service.get_material(device_uuid).sites
        notices = events.MaterialNotices(service)
        before = notices.latest()

        created = service.create_tree(
            mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[MaterialNodeCreate(client_ref="sample", identity=MaterialIdentityWrite(
                    resource_id=f"sample-{uuid4().hex[:8]}", template_name="sample_tpl", config={"type": "Container"},
                ))],
                placement=MaterialPlacement(parent_material_uuid=device_uuid, site=site.site_uuid),
            ),
        )
        sample_uuid = created.data.root_material_uuid
        after = notices.latest()
        assert after > before
        assert set(notices.changed_materials(before, after)) == {sample_uuid, device_uuid}
    finally:
        service.close()


def test_notices_exist_only_where_the_materials_authority_lives(monkeypatch):
    from unilabos.server import execution_composition

    monkeypatch.setattr(execution_composition, "get_materials_service", lambda: None)
    assert events.material_notices() is None

    monkeypatch.setattr(execution_composition, "get_materials_service", lambda: _Ledger(12))
    notices = events.material_notices()
    assert notices is not None and notices.latest() == 12
