"""以真实库存事务验证来源、恢复和状态通道；不启动设备或引擎。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.runtime.observations import Descriptor, Property, Sample, Selector, StateError
from unilabos.testing.observations import observation_services


def seed_legacy_hold(runtime, job_id, *, group="group", task_id="task", identifier="axis"):
    """模拟升级前已落盘的阻断；生产 API 不允许创建这些历史行。"""
    source = runtime.observations.snapshot()["groups"][group]
    node = runtime.observations.resolve(Selector(value=identifier))
    record = dict(group=group, task_id=task_id, job_id=job_id, material_uuid=node["uuid"],
                  generation=node["generation"], epoch=source["epoch"], provider=source["provider"],
                  source_session=source["source_session"], runtime_session=runtime.context.session,
                  state="pending", evidence=None)
    with runtime.context.transaction():
        runtime.store.db.execute("INSERT INTO observation_legacy_hold VALUES (?,?)", (job_id, json.dumps(record)))
        source["pending"].append(job_id)
        runtime.context.save_group(group, source)
    return record


def test_provider_config_cannot_turn_the_real_edge_into_a_simulator():
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.simulation.dispatch import SourceDispatcher
    from unilabos.simulation.provider import ProviderControl

    delegate = RecordingDispatcher()
    from unilabos.simulation.dispatch import SourceDispatcher
    dispatcher = SourceDispatcher(delegate, None, None)
    with pytest.raises(StateError) as error:
        ProviderControl(None, dispatcher, {"group": "cell", "provider": "simulation",
            "versions": {"axis": "1"}, "actions": {"axis": ["move"]}}, sources=None)
    assert error.value.reason == "simulation_route_required"
    assert not dispatcher.routes
    assert not delegate.dispatched


@pytest.fixture
def world(tmp_path):
    inventory = InventoryStore(str(tmp_path / "inventory.db"))
    service = BackendResourceService(inventory)
    template = service.sync_resource_templates([{"id": "axis_test", "registry_type": "resource"}])["templates"][0][
        "uuid"
    ]

    def create(identifier="axis", name="Axis", **kwargs):
        return service.create_material(
            {
                "resource_template_uuid": template,
                "name": name,
                "meta_data": {"source": "resource-tree-set", "source_node_id": identifier},
                **kwargs,
            }
        )

    material = create()
    clock = [100.0]
    runtime = observation_services(inventory, str(tmp_path / "state.db"), clock=lambda: clock[0], retention=10)
    descriptor = Descriptor(
        version="1",
        properties={
            "position.observed": Property(semantic="observed", unit="m", frame="local", lower=0, upper=1),
            "position.goal": Property(semantic="goal", unit="m", frame="local", lower=0, upper=1),
        },
    )
    node = runtime.observations.describe(Selector(value="axis"), descriptor)
    runtime.sources.configure_group("group", [Selector(value="axis")])
    operation = runtime.sources.switch(
        "group",
        operation_id="first",
        mode="simulation",
        provider="physics",
        source_session="source-1",
        prepare=lambda: None,
        quiescent=lambda: True,
    )
    runtime.sources.ready("group", node["uuid"], operation["epoch"], "source-1")

    def sample(**kwargs):
        return Sample(
            **{
                **dict(
                    material_uuid=node["uuid"],
                    generation=node["generation"],
                    descriptor_version="1",
                    group="group",
                    epoch=operation["epoch"],
                    runtime_session=runtime.context.session,
                    source_session="source-1",
                    provider="physics",
                    property="position.observed",
                    seq=0,
                    sampled_at=0.0,
                    clock="simulation",
                    quality="good",
                    value=0.5,
                ),
                **kwargs,
            }
        )

    yield runtime, service, create, clock, node, descriptor, sample, tmp_path
    runtime.store.close()
    inventory.close()


def reason(expected, fn):
    with pytest.raises(StateError) as error:
        fn()
    assert error.value.reason == expected


def test_bound_native_template_drift_is_rejected(world):
    runtime, _, _, _, node, descriptor, sample, _ = world
    runtime.observations.publish(sample())
    baseline = runtime.observations.snapshot()
    changed = descriptor.model_copy(update={"version": "2"})
    with runtime.inventory.transaction() as connection:
        connection.execute(
            "UPDATE resource_template SET meta_data=? WHERE uuid=?",
            (json.dumps({"twin": changed.model_dump()}), node["resource_template_uuid"]),
        )
    assert any(
        event["kind"] == "scene.invalidated" for event in runtime.observations.changes(runtime.context.session, baseline["cursor"])["events"]
    )
    reason("version_incompatible", runtime.observations.snapshot)
    reason("version_incompatible", lambda: runtime.observations.publish(sample(seq=1)))


def test_native_identity_explicit_name_and_no_fallback(world):
    runtime, _, create, _, node, _, _, _ = world
    assert runtime.observations.resolve(Selector(by="name", value="Axis"))["uuid"] == node["uuid"]
    reason("identity_missing", lambda: runtime.observations.resolve(Selector(value="Axis")))
    parent = create("parent", "Parent")
    create("other", "Axis", parent_uuid=parent["uuid"])
    reason("identity_ambiguous", lambda: runtime.observations.resolve(Selector(by="name", value="Axis")))


def test_target_and_observation_remain_distinct_and_unknown(world):
    runtime, _, _, _, _, _, sample, _ = world
    runtime.observations.publish(sample())
    properties = runtime.observations.snapshot()["nodes"][0]["properties"]
    assert properties["position.goal"] == {"value": None, "quality": "unknown"}
    assert properties["position.observed"]["value"] == 0.5
    runtime.observations.publish(sample(property="position.goal", value=0.8))
    assert runtime.observations.snapshot()["nodes"][0]["properties"]["position.observed"]["value"] == 0.5


def test_stationary_new_sample_refreshes_liveness_and_keeps_history(world):
    runtime, _, _, clock, _, _, sample, _ = world
    baseline = runtime.observations.snapshot()
    runtime.observations.publish(sample())
    clock[0] += 2
    assert runtime.observations.snapshot()["nodes"][0]["properties"]["position.observed"]["quality"] == "stale"
    runtime.observations.publish(sample(seq=1, sampled_at=0.05))
    assert runtime.observations.snapshot()["nodes"][0]["properties"]["position.observed"]["quality"] == "good"
    updates = runtime.observations.changes(runtime.context.session, baseline["cursor"])["events"]
    assert [event["data"]["sample"]["seq"] for event in updates if event["kind"] == "property.sample"] == [0, 1]


def test_duplicate_does_not_refresh_ttl_or_reapply(world):
    runtime, _, _, clock, _, _, sample, _ = world
    runtime.observations.publish(sample())
    clock[0] += 2
    assert runtime.observations.publish(sample()) == {"accepted": False, "duplicate": True}
    assert runtime.observations.snapshot()["nodes"][0]["properties"]["position.observed"]["quality"] == "stale"
    reason("sample_out_of_order", lambda: runtime.observations.publish(sample(value=0.6)))


@pytest.mark.parametrize(
    "change", [{"epoch": 2}, {"source_session": "old"}, {"runtime_session": "old"}, {"provider": "viewer"}]
)
def test_reject_old_source_at_final_application(world, change):
    runtime, _, _, _, _, _, sample, _ = world
    reason("stale_source", lambda: runtime.observations.publish(sample(**change)))
    assert runtime.observations.snapshot()["nodes"][0]["properties"]["position.observed"]["quality"] == "unknown"


def test_delete_recreate_does_not_accept_previous_instance(world):
    runtime, service, create, _, node, _, sample, _ = world
    service.delete_material(node["uuid"])
    replacement = create()
    assert replacement["uuid"] != node["uuid"]
    reason("identity_changed", lambda: runtime.observations.publish(sample()))
    assert runtime.observations.resolve(Selector(value="axis"))["generation"] != node["generation"]


def test_control_range_rejects_but_observation_preserved(world):
    runtime, _, _, _, _, _, sample, _ = world
    reason("target_out_of_range", lambda: runtime.observations.publish(sample(property="position.goal", value=2.0)))
    assert runtime.observations.publish(sample(value=2.0))["out_of_range"] is True
    assert runtime.observations.snapshot()["nodes"][0]["properties"]["position.observed"]["value"] == 2.0


def test_independent_component_time_is_not_rewritten(world):
    runtime, _, _, _, _, _, sample, _ = world
    runtime.observations.publish(sample(sampled_at=1.0))
    runtime.observations.publish(sample(property="position.goal", sampled_at=2.0))
    properties = runtime.observations.snapshot()["nodes"][0]["properties"]
    assert properties["position.observed"]["sample"]["sampled_at"] == 1
    assert properties["position.goal"]["sample"]["sampled_at"] == 2
    reason("sample_time_invalid", lambda: runtime.observations.publish(sample(seq=1, sampled_at=0.0)))


def test_mode_busy_and_prepare_failure_leave_previous_source(world):
    runtime, _, _, _, _, _, sample, _ = world
    args = dict(operation_id="second", mode="replay", provider="record", source_session="next")
    reason("group_busy", lambda: runtime.sources.switch("group", **args, prepare=lambda: None, quiescent=lambda: False))

    def failure():
        raise RuntimeError("missing asset")

    reason("prepare_failed", lambda: runtime.sources.switch("group", **args, prepare=failure, quiescent=lambda: True))
    assert runtime.observations.publish(sample())["accepted"]


def test_commit_blocks_until_ready_and_retry_is_idempotent(world):
    runtime, _, _, _, node, _, sample, _ = world
    args = dict(
        operation_id="second",
        mode="replay",
        provider="record",
        source_session="next",
        prepare=lambda: None,
        quiescent=lambda: True,
    )
    first = runtime.sources.switch("group", **args)
    assert runtime.sources.switch("group", **args) == first
    reason("stale_source", lambda: runtime.observations.publish(sample()))
    new = sample(epoch=first["epoch"], provider="record", source_session="next")
    reason("group_not_ready", lambda: runtime.observations.publish(new))
    runtime.sources.ready("group", node["uuid"], first["epoch"], "next")
    runtime.observations.publish(new)
    runtime.sources.disconnect("group")
    assert runtime.observations.snapshot()["groups"]["group"]["provider"] == "record"
    reason("group_not_ready", lambda: runtime.observations.publish(new.model_copy(update={"seq": 1})))


def test_restart_restores_native_graph_but_blocks_old_source(world):
    runtime, _, _, _, node, _, sample, path = world
    runtime.observations.publish(sample())
    restarted = observation_services(runtime.inventory, str(path / "state.db"))
    try:
        snapshot = restarted.observations.snapshot()
        assert snapshot["nodes"][0]["uuid"] == node["uuid"]
        assert snapshot["groups"]["group"]["recovery_required"]
        assert not snapshot["groups"]["group"]["ready"]
        reason("stale_source", lambda: restarted.observations.publish(sample()))
        reason("resync_required", lambda: restarted.observations.changes(runtime.context.session, 0))
        reason("runtime_superseded", lambda: runtime.observations.publish(sample(seq=1)))
    finally:
        restarted.store.close()


def test_slow_reader_must_resync_and_structure_change_visible(world):
    runtime, _, create, _, _, _, sample, _ = world
    baseline = runtime.observations.snapshot()
    for seq in range(12):
        runtime.observations.publish(sample(seq=seq, sampled_at=seq / 20))
    reason("resync_required", lambda: runtime.observations.changes(runtime.context.session, baseline["cursor"]))
    assert len(runtime.observations.records()["records"]) == 12
    baseline = runtime.observations.snapshot()
    create("second", "Second")
    changes = runtime.observations.changes(runtime.context.session, baseline["cursor"])
    assert changes["events"][-1]["kind"] == "scene.invalidated"
    assert len(runtime.observations.snapshot()["nodes"]) == 2


def test_two_groups_do_not_cross_write(world):
    runtime, _, create, _, node, descriptor, sample, _ = world
    create("second", "Second")
    second = runtime.observations.describe(Selector(value="second"), descriptor)
    runtime.sources.configure_group("second", [Selector(value="second")])
    reason(
        "identity_changed",
        lambda: runtime.observations.publish(sample(material_uuid=second["uuid"], generation=second["generation"])),
    )
    reason("shared_dependency", lambda: runtime.sources.configure_group("overlap", [Selector(value="axis")]))


def test_duplicate_concurrent_delivery_applies_once(world):
    runtime, _, _, _, _, _, sample, _ = world
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: runtime.observations.publish(sample()), range(8)))
    assert sum(result["accepted"] for result in results) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"value": float("nan")},
        {"value": None},
        {"quality": "unknown", "value": 0.0},
        {"seq": True},
        {"schema_version": 2},
    ],
)
def test_invalid_wire_sample_rejected(world, change):
    sample = world[6]
    with pytest.raises(ValidationError):
        sample(**change)


def test_read_only_api_and_machine_readable_errors(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.scheduler.observation_api import create_observation_router

    runtime = world[0]
    app = FastAPI()
    app.include_router(create_observation_router(lambda: runtime.observations, lambda: runtime.sources), prefix="/api/v1")
    client = TestClient(app)
    snapshot = client.get("/api/v1/device-state/observations/snapshot").json()
    assert snapshot["code"] == 0
    missing = client.get("/api/v1/device-state/observations/resolve", params={"value": "missing"}).json()
    assert missing["error"]["reason"] == "identity_missing"
    assert client.post("/api/v1/device-state/observations/samples", json={}).status_code == 404
    assert (
        client.get("/api/v1/device-state/observations/changes", params={"runtime_session": "old", "after": 0}).json()["error"]["reason"]
        == "resync_required"
    )


def test_descriptor_binding_uses_native_template_uuid(world):
    from unilabos.simulation.discovery import bind_descriptors

    runtime, _, create, _, _, descriptor, _, _ = world
    new = create("second", "Second")
    template = runtime.observations.resolve(Selector(value="second"))["resource_template_uuid"]
    bound = bind_descriptors(runtime.observations, {template: {"metadata": {"twin": descriptor.model_dump()}}})
    assert new["uuid"] in bound
    assert runtime.observations.snapshot()["nodes"][0]["descriptor"] is not None


def test_create_then_delete_between_reads_still_invalidates(world):
    runtime, service, create, _, _, _, _, _ = world
    baseline = runtime.observations.snapshot()
    temporary = create("temporary", "Temporary")
    service.delete_material(temporary["uuid"])
    assert runtime.observations.snapshot()["graph_version"] != baseline["graph_version"]
    events = runtime.observations.changes(runtime.context.session, baseline["cursor"])["events"]
    assert any(event["kind"] == "scene.invalidated" for event in events)


def test_batch_invalid_component_rolls_back_all(world):
    runtime, _, _, _, _, _, sample, _ = world
    reason("target_out_of_range", lambda: runtime.observations.publish_batch([sample(), sample(property="position.goal", value=5)]))
    assert runtime.observations.records()["records"] == []
    assert runtime.observations.snapshot()["nodes"][0]["properties"]["position.observed"]["quality"] == "unknown"


def test_legacy_pending_action_still_blocks_switch_and_restart(world):
    runtime, _, _, _, _, _, _, path = world
    admission = seed_legacy_hold(runtime, "job")
    reason(
        "group_busy",
        lambda: runtime.sources.switch(
            "group",
            operation_id="busy",
            mode="replay",
            provider="record",
            source_session="next",
            prepare=lambda: None,
            quiescent=lambda: True,
        ),
    )
    restarted = observation_services(runtime.inventory, str(path / "state.db"))
    try:
        reason("reconciliation_required", lambda: restarted.sources.recover("group", quiescent=lambda: True))
        reason("canonical_inventory_receipt_missing", lambda: restarted.sources.reconcile_legacy_action(
            "job", evidence={"receipt_id": "invented"}, quiescent=lambda: True))
        assert restarted.sources.legacy_action("job")["runtime_session"] == admission["runtime_session"]
        assert restarted.observations.snapshot()["groups"]["group"]["pending"] == ["job"]
    finally:
        restarted.store.close()


def test_optional_dispatch_gate_never_falls_back_for_managed_device(world):
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.simulation.dispatch import SourceDispatcher

    runtime, service, create, _, node, _, _, _ = world
    legacy = RecordingDispatcher()
    gate = SourceDispatcher(legacy, runtime.observations, runtime.sources)
    payload = {"device_id": "axis", "task_id": "task", "job_id": "job"}
    reason("execution_route_unavailable", lambda: gate.dispatch(payload))
    assert legacy.dispatched == []
    dispatched = []
    gate.register_simulation_route("physics", lambda payload, permit: dispatched.append((payload, permit)))
    gate.dispatch(payload)
    assert len(dispatched) == 1
    assert dispatched[0][1]["provider"] == "physics"
    assert "job_id" not in dispatched[0][1]
    assert runtime.observations.snapshot()["groups"]["group"]["pending"] == []
    # 该门只选来源；跨重启的动作去重由原生命令日志测试覆盖。
    reason("action_missing", lambda: runtime.sources.legacy_action("job"))
    service.delete_material(node["uuid"])
    create()
    reason("identity_changed", lambda: gate.dispatch({**payload, "job_id": "another"}))
    assert legacy.dispatched == []


def test_operation_query_reports_degraded_state(world):
    runtime, *_ = world
    runtime.sources.disconnect("group")
    state = runtime.sources.operation("first")
    assert state["receipt"]["status"] == "committed"
    assert not state["current"]["ready"]


def test_actual_loopback_http_snapshot_reads_same_native_inventory(world, monkeypatch):
    import socket
    import threading
    import time
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    import uvicorn
    from fastapi import FastAPI

    from unilabos.app.scheduler import integration
    from unilabos.app.scheduler.api import create_scheduler_router
    from unilabos.simulation.display import StateClient

    runtime, _, create, _, _, _, sample, _ = world
    monkeypatch.setattr(integration, "get_device_observations", lambda: runtime.observations)
    monkeypatch.setattr(integration, "get_observation_sources", lambda: runtime.sources)
    monkeypatch.setattr(integration, "get_edge_backend", lambda: __import__("types").SimpleNamespace(api_key="test"))
    app = FastAPI()
    app.include_router(create_scheduler_router(lambda: None))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        base = f"http://127.0.0.1:{port}/api/v1"
        client = StateClient(base + "/device-state/observations")

        def post(value):
            request = Request(
                base + "/device-state/observations/report",
                data=json.dumps({"device_id": "axis", "samples": [value.model_dump()]}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
            )
            with urlopen(request, timeout=5) as response:
                return json.load(response)

        assert post(sample())["code"] == 0
        assert client.snapshot()["nodes"][0]["properties"]["position.observed"]["value"] == 0.5
        with pytest.raises(HTTPError) as retired:
            StateClient(base + "/twin").snapshot()
        assert retired.value.code == 404
        with urlopen(base + "/device-state/observations/resolve?value=axis", timeout=5) as response:
            assert json.load(response)["data"]["id"] == "axis"
        with urlopen(base + "/device-state/observations/events", timeout=5) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.readline() == b"event: snapshot\n"
            assert json.loads(response.readline().removeprefix(b"data: "))["nodes"][0]["id"] == "axis"
        with pytest.raises(HTTPError) as failure:
            post(sample(seq=1, source_session="old-source"))
        assert failure.value.code == 409
        assert client.snapshot()["nodes"][0]["properties"]["position.observed"]["value"] == 0.5
        create("second", "Second")
        assert len(client.snapshot()["nodes"]) == 2
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_descriptor_cache_tracks_new_description_and_failed_transaction(world, monkeypatch):
    """无描述实例的缓存可刷新，失败描述事务不得泄漏到下次快照。"""
    runtime, _, create, _, _, descriptor, _, _ = world
    second = create('second', 'Second')
    def projected():
        return next(n for n in runtime.observations.snapshot()['nodes'] if n['uuid']==second['uuid'])
    assert projected()['descriptor'] is None
    original_event=runtime.context.event
    def fail_descriptor_event(kind, value):
        if kind=='descriptor.changed':raise RuntimeError('fixture transaction failure')
        return original_event(kind,value)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.context,'event',fail_descriptor_event)
        with pytest.raises(RuntimeError,match='fixture transaction failure'):
            runtime.observations.describe(Selector(value='second'),descriptor)
    assert projected()['descriptor'] is None
    runtime.observations.describe(Selector(value='second'),descriptor)
    assert projected()['descriptor']['version']==descriptor.version


def test_provider_control_prepares_then_readies_and_filters_native_actions(world):
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.simulation.dispatch import SourceDispatcher
    from unilabos.simulation.provider import Connect, Lease, ProviderControl, Settlement

    runtime, _, _, _, node, _, _, _ = world
    delegate=RecordingDispatcher(); dispatcher=SourceDispatcher(delegate,runtime.observations, runtime.sources)
    simulated=[]
    control=ProviderControl(runtime.observations,dispatcher,{'group':'group','provider':'physics','versions':{'axis':'1'},'actions':{'axis':['simulate']}},
        simulation_dispatch=lambda payload, admission: simulated.append((payload, admission)), sources=runtime.sources)
    request=Connect(operation_id='provider-start',source_session='worker',versions={'axis':'1'})
    connected=control.connect(request); operation=connected['operation']
    assert control.connect(request)==connected
    assert not runtime.sources.operation('provider-start')['current']['ready']
    lease=Lease(runtime_session=runtime.context.session,source_session='worker',epoch=operation['epoch'])
    control.ready(lease)
    payload={'device_id':'axis','task_id':'task','job_id':'job','action':'real_action'}
    reason('execution_action_not_allowed',lambda:dispatcher.dispatch(payload))
    assert not runtime.observations.snapshot()['groups']['group']['pending']
    dispatcher.dispatch({**payload,'action':'simulate'})
    assert not delegate.dispatched
    assert len(simulated)==1 and simulated[0][1]['provider']=='physics'
    assert runtime.observations.snapshot()['groups']['group']['pending'] == []
    reason('action_missing',lambda:control.settle(Settlement(**lease.model_dump(),job_id='job',receipt_id='invented',quiescent=True)))
    reason('stale_source',lambda:control.disconnect(lease.model_copy(update={'source_session':'old'})))
    assert runtime.observations.snapshot()['groups']['group']['ready']
    control.disconnect(lease)
    assert not runtime.observations.snapshot()['groups']['group']['ready']
    reason('version_incompatible',lambda:control.connect(Connect(operation_id='bad',source_session='worker',versions={'axis':'wrong'})))


def test_provider_real_http_publishes_same_native_state(world, monkeypatch):
    import socket
    import threading
    import time

    import uvicorn
    from fastapi import FastAPI

    from unilabos.app.scheduler import integration
    from unilabos.app.scheduler.api import create_scheduler_router
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.simulation.dispatch import SourceDispatcher
    from unilabos.simulation.display import StateClient
    from unilabos.simulation.provider import ProviderControl
    from unilabos.simulation.provider_client import ObservationPublisher

    runtime, *_ = world
    control=ProviderControl(runtime.observations,SourceDispatcher(RecordingDispatcher(),runtime.observations, runtime.sources),
        {'group':'group','provider':'physics','versions':{'axis':'1'},'actions':{'axis':['simulate']}},
        simulation_dispatch=lambda payload, admission: None, sources=runtime.sources)
    monkeypatch.setattr(integration, 'get_device_observations', lambda: runtime.observations)
    monkeypatch.setattr(integration, 'get_observation_sources', lambda: runtime.sources)
    monkeypatch.setattr(integration, 'get_edge_backend', lambda: __import__('types').SimpleNamespace(api_key='test'))
    monkeypatch.setattr(integration,'get_simulation_provider_control',lambda:control)
    app=FastAPI();app.include_router(create_scheduler_router(lambda:None))
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        deadline=time.monotonic()+5
        while not server.started:
            assert time.monotonic()<deadline
            time.sleep(.01)
        url=f'http://127.0.0.1:{port}/api/v1'
        publisher=ObservationPublisher(url,versions={'axis':'1'},operation_id='http-provider',source_session='http-worker',api_key='test')
        publisher.ready()
        publisher.publish(.05,{'axis':{'position.observed':.75}},sample_group='tick1',identity={'task_uuid':'task','node_job_uuid':'job'},request_id='job')
        client=StateClient(url+'/device-state/observations');node=client.snapshot()['nodes'][0]
        assert node['properties']['position.observed']['value']==.75
        assert node['properties']['position.observed']['sample']['job_id']=='job'
        publisher.close()
        assert client.snapshot()['nodes'][0]['properties']['position.observed']['quality']=='stale'
    finally:
        server.should_exit=True;thread.join(timeout=5);sock.close()


def test_observation_routing_preserves_native_edge_routes(world, tmp_path):
    """可选派发门不能遮蔽原生控制面类型，否则服务器不会挂载 Edge 会话路由。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.edge_control.local_authority import (
        LocalEdgeAuthorityStore,
        LocalEdgeControlAuthority,
        create_local_edge_control_router,
    )
    from unilabos.app.scheduler.stack import create_edge_stack
    from unilabos.simulation.dispatch import SourceDispatcher

    runtime = world[0]
    authority = LocalEdgeControlAuthority(LocalEdgeAuthorityStore(tmp_path / 'authority.db'), api_key='test')
    scheduler, backend = create_edge_stack(execution_backend=authority, observations=runtime.observations,
                                           observation_sources=runtime.sources)
    try:
        assert backend is authority
        from unilabos.simulation.dispatch import SourceDispatcher
        assert isinstance(scheduler._dispatcher, SourceDispatcher)
        app = FastAPI()
        if isinstance(backend, LocalEdgeControlAuthority):
            app.include_router(create_local_edge_control_router(backend))
        with TestClient(app) as client:
            response = client.post('/api/v1/edge/sessions', json={})
        assert response.status_code != 404
    finally:
        backend.stop()


@pytest.mark.parametrize('pending', [False, True])
def test_provider_restart_recovery_is_explicit_and_idempotent(world, pending):
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.simulation.dispatch import SourceDispatcher
    from unilabos.simulation.provider import Connect, Lease, ProviderControl, Recovery

    runtime, _, _, clock, _, _, _, path = world
    if pending:
        seed_legacy_hold(runtime, 'pending-job')
    old_session=runtime.context.session
    runtime.store.close()
    restored=observation_services(runtime.inventory,str(path/'state.db'),clock=lambda:clock[0])
    try:
        config={'group':'group','provider':'physics','versions':{'axis':'1'},'actions':{'axis':['move']}}
        control=ProviderControl(restored.observations,SourceDispatcher(RecordingDispatcher(),restored.observations, restored.sources),config,
            simulation_dispatch=lambda payload, admission: None, sources=restored.sources)
        group=restored.observations.snapshot()['groups']['group']
        assert group['recovery_required'] and not group['ready'] and restored.context.session!=old_session
        request=Recovery(operation_id='recover',runtime_session=restored.context.session,
                         source_session=group['source_session'],epoch=group['epoch'],quiescent=True)
        reason('stale_source',lambda:control.recover(request.model_copy(update={'runtime_session':old_session})))
        if pending:
            reason('reconciliation_required',lambda:control.recover(request))
            reason('canonical_inventory_receipt_missing',lambda:control.recover(request.model_copy(update={'receipts':{'pending-job':'invented'}})))
            assert restored.sources.legacy_action('pending-job')['state']=='pending'
        else:
            result=control.recover(request)
            assert control.recover(request)==result
            assert restored.observations.snapshot()['groups']['group']['epoch']==group['epoch']+1
            reason('operation_conflict',lambda:control.recover(request.model_copy(update={'epoch':99})))
            assert not result['ready'] and result['provider'] is None
            connected=control.connect(Connect(operation_id='new-source',source_session='new',versions={'axis':'1'}))['operation']
            lease=Lease(**{key:connected[key] for key in ('runtime_session','source_session','epoch')})
            control.ready(lease)
            assert restored.observations.snapshot()['groups']['group']['ready']
    finally:
        restored.store.close()


def test_read_only_operation_and_action_queries(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.scheduler.observation_api import create_observation_router
    runtime, *_ = world
    seed_legacy_hold(runtime, 'pending-job')
    app=FastAPI();app.include_router(create_observation_router(lambda: runtime.observations, lambda: runtime.sources))
    with TestClient(app) as client:
        operation=client.get('/device-state/observations/operations/first').json()['data']
        action=client.get('/device-state/observations/actions/pending-job').json()['data']
    assert operation['current']['pending']==['pending-job']
    assert action['state']=='pending' and action['evidence'] is None
