from __future__ import annotations

import hashlib
import json
import logging
import shlex
import signal
import stat
import threading
from pathlib import Path

import pytest

import scripts.szlab_task_opc_simulator as simulator
from scripts.szlab_task_opc_simulator import (
    DEFAULT_PROFILE_PATH,
    DEFAULT_URL,
    GlobalTimeoutError,
    SimulatorConfig,
    TaskOpcStateMachine,
    TrackedWriter,
    load_simulator_profile,
    run_simulator,
    validate_url,
)


def var(name, direction, data_type, initial=..., source="manual"):
    result = dict(name=name, direction=direction, data_type=data_type, source=source)
    if initial is not ...:
        result["initial_value"] = initial
    return result


def cond(name, value, edge="level"):
    return {"all": [{"variable": name, "operator": "eq", "value": value, "edge": edge}]}


def write(name, value):
    return {"variable": name, "value": value}


def payload():
    return {
        "schema_version": 2,
        "status": "runnable",
        "name": "generic-test",
        "opc": {
            "url": DEFAULT_URL,
            "poll_interval": 0.2,
            "io_timeout": 2.0,
        },
        "variables": [
            var("command", "pc_to_plc", "bool"),
            var("number", "pc_to_plc", "int"),
            var("done", "plc_to_pc", "int", 0),
            var("ready", "plc_to_pc", "bool", True),
            var("other_command", "pc_to_plc", "bool"),
            var("other_done", "plc_to_pc", "bool", False),
        ],
        "nodes": [
            {
                "workflow_node_id": "a",
                "task_template_ids": ["ta"],
                "device_id": "da",
                "method": "anything",
                "params": {"opaque": 1},
                "channel": "a",
                "trigger": {
                    "all": [
                        {"variable": "command", "operator": "eq", "value": True, "edge": "rising"},
                        {"variable": "number", "operator": "eq", "value": 7, "edge": "level"},
                    ]
                },
                "on_trigger": {"writes": [write("ready", False), write("done", 0)]},
                "on_complete": {"delay": 2, "writes": [write("done", 7), write("ready", True)]},
                "reset_when": cond("command", False),
                "after_reset": {"delay": 1, "writes": [write("done", 0)]},
            },
            {
                "workflow_node_id": "b",
                "task_template_ids": ["tb"],
                "device_id": "db",
                "method": "same-engine",
                "params": {},
                "channel": "b",
                "trigger": cond("other_command", True, "rising"),
                "on_trigger": {"writes": [write("other_done", False)]},
                "on_complete": {"delay": 2, "writes": [write("other_done", True)]},
                "reset_when": cond("other_command", False),
                "after_reset": {"delay": 0, "writes": []},
            },
        ],
    }


def profile_path(tmp_path, value):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def ops(machine, snapshot, now):
    return [(op.name, op.value) for op in machine.tick(snapshot, now)]


def test_default_profile_is_runnable_schema_v2_with_six_generic_nodes():
    profile = load_simulator_profile(DEFAULT_PROFILE_PATH)
    assert (profile.schema_version, profile.status, profile.url) == (2, "runnable", DEFAULT_URL)
    assert len(profile.nodes) == 6
    assert {node.channel for node in profile.nodes} == {"robot", "s07", "s06"}
    sources = {variable.name: variable.source for variable in profile.variables}
    assert sources["任务号"] == "action_node"
    assert sources["S07工艺选择"] == "action_node"
    assert sources["S06工艺选择"] == "action_node"
    assert sources["Robot_Home"] == "manual"


def test_profile_expected_revision_matches_single_read_bytes(tmp_path):
    path = profile_path(tmp_path, payload())
    content = path.read_bytes()
    revision = hashlib.sha256(content).hexdigest()

    profile = load_simulator_profile(path, expected_revision=revision)

    assert profile.name == "generic-test"


def test_profile_revision_mismatch_fails_before_json_validation(tmp_path):
    path = tmp_path / "profile.json"
    path.write_bytes(b"not json")

    with pytest.raises(ValueError, match="revision 不匹配"):
        load_simulator_profile(path, expected_revision="0" * 64)


def test_profile_replace_race_validates_and_parses_the_same_bytes(
    tmp_path, monkeypatch
):
    path = profile_path(tmp_path, payload())
    original = path.read_bytes()
    replacement = payload()
    replacement["name"] = "replacement"
    replacement_bytes = json.dumps(replacement).encode()
    original_read_bytes = Path.read_bytes
    reads = 0

    def replace_after_read(candidate):
        nonlocal reads
        if candidate != path:
            return original_read_bytes(candidate)
        reads += 1
        content = original_read_bytes(candidate)
        candidate.write_bytes(replacement_bytes)
        return content

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)

    profile = load_simulator_profile(
        path,
        expected_revision=hashlib.sha256(original).hexdigest(),
    )

    assert reads == 1
    assert profile.name == "generic-test"
    assert json.loads(original_read_bytes(path))["name"] == "replacement"


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda p: p.update(schema_version=1), "schema_version"),
        (lambda p: p.update(status="bad"), "status"),
        (lambda p: p["variables"][0].update(direction="unknown"), "variables[0].direction"),
        (lambda p: p["variables"][1].update(name="command"), "variables[1].name"),
        (lambda p: p["variables"][0].update(initial_value=False), "variables[0].initial_value"),
        (lambda p: p["nodes"][0].update(channel=""), "nodes[0].channel"),
        (lambda p: p["nodes"][0].update(trigger={"all": []}), "nodes[0].trigger.all"),
        (lambda p: p["nodes"][0]["trigger"]["all"][0].update(variable="missing"), "nodes[0].trigger.all[0].variable"),
        (lambda p: p["nodes"][0]["on_trigger"]["writes"][0].update(
            variable="command"), "nodes[0].on_trigger.writes[0].variable"),
        (lambda p: p["nodes"][0]["on_complete"]["writes"][0].update(
            value=True), "nodes[0].on_complete.writes[0].value"),
        (lambda p: p["nodes"][0]["on_complete"].update(delay=float("nan")), "nodes[0].on_complete.delay"),
        (lambda p: p["nodes"][0]["on_complete"].pop("delay"), "nodes[0].on_complete.delay"),
        (lambda p: p["nodes"][0]["after_reset"].update(delay=-1), "nodes[0].after_reset.delay"),
    ],
)
def test_runnable_rejects_invalid_profile_with_exact_path(tmp_path, mutate, path):
    value = payload()
    mutate(value)
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert path in str(error.value)


def test_draft_returns_exact_path_errors_instead_of_raising(tmp_path):
    value = {"schema_version": 2, "status": "draft", "variables": [{"name": "", "direction": "unknown"}], "nodes": [{}]}
    profile = load_simulator_profile(profile_path(tmp_path, value))
    assert {"name", "opc", "variables[0].name", "variables[0].data_type", "variables[0].source",
            "nodes[0].channel", "nodes[0].trigger"} <= set(profile.validation_errors)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p["opc"].pop("poll_interval"), "opc.poll_interval"),
        (lambda p: p["opc"].pop("io_timeout"), "opc.io_timeout"),
        (lambda p: p["opc"].update(poll_interval=0.049), "opc.poll_interval"),
        (lambda p: p["opc"].update(poll_interval=61), "opc.poll_interval"),
        (lambda p: p["opc"].update(io_timeout=0.099), "opc.io_timeout"),
        (lambda p: p["opc"].update(io_timeout=61), "opc.io_timeout"),
        (lambda p: p["opc"].update(poll_interval=float("nan")), "opc.poll_interval"),
        (lambda p: p["opc"].update(io_timeout=float("inf")), "opc.io_timeout"),
    ],
)
def test_schema_v2_requires_bounded_finite_opc_timings(tmp_path, mutate, expected):
    value = payload()
    mutate(value)
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert expected in str(error.value)


@pytest.mark.parametrize("source", ["input", "output", "test", "", 1, None])
def test_schema_v2_rejects_noncanonical_variable_source(tmp_path, source):
    value = payload()
    value["variables"][0]["source"] = source
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert "variables[0].source" in str(error.value)


def test_draft_accepts_unknown_action_node_variable_but_runnable_rejects_it(
    tmp_path,
):
    value = payload()
    value["status"] = "draft"
    value["variables"][0].update(
        data_type="unknown",
        source="action_node",
    )
    value["variables"][0].pop("initial_value", None)

    draft = load_simulator_profile(profile_path(tmp_path, value))

    assert draft.variables[0].data_type == "unknown"
    assert draft.variables[0].source == "action_node"
    assert "variables[0].data_type" in draft.validation_errors

    value["status"] = "runnable"
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert "variables[0].data_type" in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "opc.tcp://127.0.0.1:4840/custom",
        "opc.tcp://localhost",
        "opc.tcp://[::1]:4840",
    ],
)
def test_schema_v2_accepts_arbitrary_legal_opc_tcp_url(tmp_path, url):
    value = payload()
    value["opc"]["url"] = url
    assert load_simulator_profile(profile_path(tmp_path, value)).url == url


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p.update(name="x" * 257), "name"),
        (
            lambda p: p["variables"][0].update(name="x" * 513),
            "variables[0].name",
        ),
        (
            lambda p: p["nodes"][0].update(workflow_node_id="x" * 257),
            "nodes[0].workflow_node_id",
        ),
        (
            lambda p: p["nodes"][0].update(device_id="x" * 257),
            "nodes[0].device_id",
        ),
        (
            lambda p: p["nodes"][0].update(method="x" * 257),
            "nodes[0].method",
        ),
        (
            lambda p: p["nodes"][0].update(task_template_ids=["x" * 257]),
            "nodes[0].task_template_ids[0]",
        ),
    ],
)
def test_schema_v2_rejects_overlong_names_and_identifiers(
    tmp_path,
    mutate,
    expected,
):
    value = payload()
    mutate(value)

    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))

    assert expected in str(error.value)


@pytest.mark.parametrize(
    "url",
    ["http://localhost:4840", "opc.tcp://", "opc.tcp://host:99999", "not-a-url"],
)
def test_schema_v2_rejects_invalid_opc_tcp_url(tmp_path, url):
    value = payload()
    value["opc"]["url"] = url
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert "opc.url" in str(error.value)


def test_eq_edges_all_group_prime_and_generic_engine(tmp_path):
    machine = TaskOpcStateMachine(profile=load_simulator_profile(profile_path(tmp_path, payload())))
    low = {"command": False, "number": 7, "other_command": False}
    high = dict(low, command=True)
    assert ops(machine, high, 0) == []
    assert ops(machine, high, 0.1) == []
    assert ops(machine, low, 0.2) == []
    assert ops(machine, dict(high, number=8), 0.3) == []
    ops(machine, low, 0.4)
    assert ops(machine, high, 0.5) == [("ready", False), ("done", 0)]


def test_completion_reset_after_reset_and_channel_concurrency(tmp_path):
    machine = TaskOpcStateMachine(profile=load_simulator_profile(profile_path(tmp_path, payload())))
    low = {"command": False, "number": 7, "other_command": False}
    both = dict(low, command=True, other_command=True)
    machine.prime(low)
    assert ops(machine, both, 0) == [("ready", False), ("done", 0), ("other_done", False)]
    ops(machine, low, 0.5)
    assert ops(machine, both, 1) == []
    assert ops(machine, both, 2) == [("done", 7), ("ready", True), ("other_done", True)]
    assert ops(machine, low, 2.1) == [("other_done", False)]
    assert ops(machine, low, 3.1) == [
        ("done", 0),
        ("ready", False),
        ("done", 0),
    ]


def test_startup_writes_all_plc_feedback_initials(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    assert {(op.name, op.value) for op in TaskOpcStateMachine(profile=profile).initial_writes()} == {
        ("done", 0), ("ready", True), ("other_done", False)}


class FakeAdapter:
    def __init__(self, values=None):
        self.values, self.writes = dict(values or {}), []

    def read(self, name):
        return self.values.get(name, False)

    def write(self, name, value):
        self.writes.append((name, value))
        self.values[name] = value


def test_restore_ownership_is_type_sensitive():
    adapter = FakeAdapter({"x": False})
    writer = TrackedWriter(adapter, logger=logging.getLogger("test"))
    writer.write("x", True)
    adapter.values["x"] = 1
    result = writer.restore()
    assert not result.success and result.skipped == ["x"] and adapter.values["x"] == 1
    assert "所有权检查失败" in result.errors[0]


def test_cli_keeps_generic_flags_and_removes_domain_flags():
    parser = simulator.build_parser()
    args = parser.parse_args([])
    assert Path(args.config) == DEFAULT_PROFILE_PATH
    assert args.url is None
    assert args.timeout is None and args.io_timeout is None and args.poll_interval is None
    assert args.log_level == "INFO" and not args.dry_run and not args.allow_unsafe_url
    with pytest.raises(SystemExit):
        parser.parse_args(["--samples", "3"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--fail-on", "anything"])


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "0" * 63,
        "0" * 65,
        "A" * 64,
        "g" * 64,
    ],
)
def test_cli_rejects_invalid_expected_revision(revision):
    with pytest.raises(SystemExit):
        simulator.build_parser().parse_args(
            ["--expected-revision", revision]
        )


def test_main_revision_mismatch_never_enters_simulator_io(
    tmp_path, monkeypatch
):
    path = profile_path(tmp_path, payload())
    entered_run = False
    error_calls = []

    class RecordingLogger:
        def error(self, message, *args):
            error_calls.append((message, args))

    class RecordingLogging:
        def __getattr__(self, name):
            return getattr(logging, name)

        def basicConfig(self, **_kwargs):
            return None

        def getLogger(self, _name=None):
            return RecordingLogger()

    def forbidden_run(*_args, **_kwargs):
        nonlocal entered_run
        entered_run = True
        raise AssertionError("revision mismatch must fail before OPC or lock I/O")

    monkeypatch.setattr(simulator, "run_simulator", forbidden_run)
    monkeypatch.setattr(simulator, "logging", RecordingLogging())

    code = simulator.main(
        [
            "--config",
            str(path),
            "--expected-revision",
            "0" * 64,
        ]
    )

    assert code != 0
    assert entered_run is False
    assert len(error_calls) == 1
    message, args = error_calls[0]
    assert message == "模拟器配置无效：%s"
    assert len(args) == 1
    assert "revision 不匹配" in str(args[0])


def test_config_uses_profile_and_operational_overrides(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    config = SimulatorConfig(profile=profile)
    assert config.url == DEFAULT_URL and config.profile is profile
    assert config.poll_interval == 0.2 and config.io_timeout == 2.0
    overridden = SimulatorConfig(profile=profile, poll_interval=0.5, io_timeout=1)
    assert overridden.poll_interval == 0.5 and overridden.io_timeout == 1


def draft_profile(tmp_path, mutate):
    value = payload()
    value["status"] = "draft"
    mutate(value)
    return load_simulator_profile(profile_path(tmp_path, value))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p["variables"][0].update(data_type=[]), "variables[0].data_type"),
        (lambda p: p["variables"][0].update(name={}), "variables[0].name"),
        (lambda p: p["variables"][0].update(direction=None), "variables[0].direction"),
        (lambda p: p["variables"][0].update(source=[]), "variables[0].source"),
        (lambda p: p["nodes"][0].update(trigger=[]), "nodes[0].trigger"),
        (lambda p: p["nodes"][0].update(on_trigger=[]), "nodes[0].on_trigger"),
        (lambda p: p["nodes"][0].update(on_complete=[]), "nodes[0].on_complete"),
        (lambda p: p["nodes"][0].update(after_reset=[]), "nodes[0].after_reset"),
        (lambda p: p["nodes"][0]["trigger"].update(all={}), "nodes[0].trigger.all"),
        (lambda p: p["nodes"][0]["on_trigger"].update(writes={}), "nodes[0].on_trigger.writes"),
        (lambda p: p["nodes"][0]["on_trigger"].update(writes=[[]]), "nodes[0].on_trigger.writes[0]"),
    ],
)
def test_draft_fuzz_types_only_return_validation_paths(tmp_path, mutate, expected):
    profile = draft_profile(tmp_path, mutate)
    assert expected in profile.validation_errors


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p.update(extra=1), "extra"),
        (lambda p: p["opc"].update(extra=1), "opc.extra"),
        (lambda p: p["variables"][0].update(extra=1), "variables[0].extra"),
        (lambda p: p["nodes"][0].update(extra=1), "nodes[0].extra"),
        (lambda p: p["nodes"][0]["trigger"].update(extra=1), "nodes[0].trigger.extra"),
        (
            lambda p: p["nodes"][0]["trigger"]["all"][0].update(extra=1),
            "nodes[0].trigger.all[0].extra",
        ),
        (lambda p: p["nodes"][0]["on_trigger"].update(extra=1), "nodes[0].on_trigger.extra"),
        (
            lambda p: p["nodes"][0]["on_trigger"]["writes"][0].update(extra=1),
            "nodes[0].on_trigger.writes[0].extra",
        ),
    ],
)
def test_runnable_forbids_extra_fields_at_every_schema_level(
    tmp_path, mutate, expected
):
    value = payload()
    mutate(value)
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert expected in str(error.value)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p.update(variables=[]), "variables"),
        (lambda p: p.update(nodes=[]), "nodes"),
        (
            lambda p: p["nodes"][1].update(workflow_node_id="a"),
            "nodes[1].workflow_node_id",
        ),
        (lambda p: p["nodes"][0].update(task_template_ids=[]), "nodes[0].task_template_ids"),
        (
            lambda p: p["nodes"][0].update(task_template_ids=[""]),
            "nodes[0].task_template_ids[0]",
        ),
        (
            lambda p: p["nodes"][0].update(reset_when=None),
            "nodes[0].after_reset",
        ),
    ],
)
def test_runnable_enforces_nonempty_unique_cross_field_rules(
    tmp_path, mutate, expected
):
    value = payload()
    mutate(value)
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert expected in str(error.value)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda p: p["nodes"][0]["trigger"]["all"][0].update(value=1),
            "nodes[0].trigger.all[0].value",
        ),
        (
            lambda p: p["nodes"][0]["trigger"]["all"][1].update(value=True),
            "nodes[0].trigger.all[1].value",
        ),
        (
            lambda p: p["nodes"][0]["on_complete"]["writes"][0].update(value=True),
            "nodes[0].on_complete.writes[0].value",
        ),
        (
            lambda p: p["variables"][2].update(initial_value=False),
            "variables[2].initial_value",
        ),
    ],
)
def test_bool_and_int_never_coerce_in_runnable_values(tmp_path, mutate, expected):
    value = payload()
    mutate(value)
    with pytest.raises(ValueError) as error:
        load_simulator_profile(profile_path(tmp_path, value))
    assert expected in str(error.value)


def test_default_profile_has_real_workflow_metadata():
    profile = load_simulator_profile(DEFAULT_PROFILE_PATH)
    actual = [
        (
            node.workflow_node_id,
            node.task_template_ids,
            node.device_id,
            node.method,
            node.params,
        )
        for node in profile.nodes
    ]
    assert actual == [
        (
            "node_001_pick_from_s03",
            ("szlab-e2e-s07",),
            "szlab_mixer_robot",
            "submit_pick_from_s03",
            {"product_type": 1, "position": "1-1"},
        ),
        (
            "node_002_place_to_s072",
            ("szlab-e2e-s07",),
            "szlab_mixer_robot",
            "submit_place_to_s072",
            {"product_type": 1, "position": 1},
        ),
        (
            "node_003_dose_powder",
            ("szlab-e2e-s07",),
            "szlab_s07_solid_addition",
            "dose_powder",
            {
                "coarse_position": 1,
                "fine_position": 1,
                "target_weight": 1.0,
                "recipe_name": "default",
                "timeout": 300.0,
            },
        ),
        (
            "node_004_pick_from_s072",
            ("szlab-e2e-s06",),
            "szlab_mixer_robot",
            "submit_pick_from_s072",
            {"product_type": 1, "position": 1},
        ),
        (
            "node_005_place_to_s06",
            ("szlab-e2e-s06",),
            "szlab_mixer_robot",
            "submit_place_to_s06",
            {},
        ),
        (
            "node_006_run_solvent_addition",
            ("szlab-e2e-s06",),
            "szlab_s06_pump",
            "run_solvent_addition",
            {"process": 3, "volume_pump_1": 1, "volume_pump_2": 1},
        ),
    ]


class SafetyAdapter(FakeAdapter):
    def __init__(self, values=None):
        super().__init__(values)
        self.closed = False
        self.reads = []

    def read(self, name):
        self.reads.append(name)
        return super().read(name)

    def close(self):
        self.closed = True


def test_url_whitelist_requires_explicit_unsafe_override():
    assert DEFAULT_URL == "opc.tcp://127.0.0.1:4840"
    assert validate_url(DEFAULT_URL, allow_unsafe=False) == DEFAULT_URL
    with pytest.raises(ValueError, match="allow-unsafe-url"):
        validate_url(
            "opc.tcp://remote.example:4840",
            allow_unsafe=False,
        )
    assert (
        validate_url(
            "opc.tcp://remote.example:4840",
            allow_unsafe_url=True,
        )
        == "opc.tcp://remote.example:4840"
    )


def test_dry_run_writer_and_runner_perform_absolutely_zero_writes(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    adapter = SafetyAdapter({"done": 9, "ready": False, "other_done": True})
    writer = TrackedWriter(adapter, dry_run=True)
    writer.write("done", 0)
    assert writer.restore().success
    assert adapter.writes == []

    stopped = False

    def wait(_seconds):
        nonlocal stopped
        stopped = True
        return True

    assert run_simulator(
        SimulatorConfig(profile=profile, dry_run=True),
        adapter_factory=lambda _config: adapter,
        stop_requested=lambda: stopped,
        interruptible_wait=wait,
    ) == 0
    assert adapter.writes == []
    assert adapter.closed


def test_preexisting_stop_never_connects():
    calls = 0

    def factory(_config):
        nonlocal calls
        calls += 1
        return SafetyAdapter()

    assert run_simulator(SimulatorConfig(), adapter_factory=factory, stop_requested=lambda: True) == 0
    assert calls == 0


def test_sigterm_handler_sets_stop_event(monkeypatch):
    handlers = {}
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: handlers.setdefault(signum, handler),
    )
    stop_event = threading.Event()
    simulator._install_stop_signals(stop_event, logging.getLogger("test"))
    assert signal.SIGTERM in handlers
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stop_event.is_set()


def test_wait_is_interruptible_and_clamped_to_global_deadline(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    waits = []
    adapter = SafetyAdapter()
    assert run_simulator(
        SimulatorConfig(profile=profile, timeout=1, poll_interval=60),
        adapter_factory=lambda _config: adapter,
        interruptible_wait=lambda seconds: waits.append(seconds) or True,
    ) == 0
    assert len(waits) == 1
    assert 0 <= waits[0] <= 1
    assert adapter.closed


@pytest.mark.parametrize(("dry_run", "expected"), [(False, 1), (True, 0)])
def test_global_timeout_exit_semantics(tmp_path, dry_run, expected):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    expired = False

    def wait(_seconds):
        nonlocal expired
        expired = True
        return False

    code = run_simulator(
        SimulatorConfig(profile=profile, timeout=1, dry_run=dry_run),
        adapter_factory=lambda _config: SafetyAdapter(),
        monotonic=lambda: 2.0 if expired else 0.0,
        interruptible_wait=wait,
    )
    assert code == expected


def test_omitted_timeout_runs_past_legacy_300_seconds_until_stopped(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    stopped = False
    now = 0.0

    def wait(_seconds):
        nonlocal stopped, now
        now = 301.0
        stopped = True
        return True

    assert run_simulator(
        SimulatorConfig(profile=profile),
        adapter_factory=lambda _config: SafetyAdapter(),
        stop_requested=lambda: stopped,
        monotonic=lambda: now,
        interruptible_wait=wait,
    ) == 0


def test_finally_restores_and_closes_after_adapter_read_exception(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))

    class ExplodingRead(SafetyAdapter):
        def read(self, name):
            if self.writes:
                raise RuntimeError("read failed")
            return super().read(name)

    adapter = ExplodingRead()
    with pytest.raises(RuntimeError, match="read failed"):
        run_simulator(
            SimulatorConfig(profile=profile),
            adapter_factory=lambda _config: adapter,
        )
    assert adapter.closed


def test_finally_restores_successful_writes_and_closes_on_wait_exception(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    originals = {"done": 9, "ready": False, "other_done": True}
    adapter = SafetyAdapter(originals)
    with pytest.raises(RuntimeError, match="wait failed"):
        run_simulator(
            SimulatorConfig(profile=profile),
            adapter_factory=lambda _config: adapter,
            interruptible_wait=lambda _seconds: (_ for _ in ()).throw(
                RuntimeError("wait failed")
            ),
        )
    assert adapter.closed
    assert {name: adapter.values[name] for name in originals} == originals


def test_restore_failure_makes_runner_nonzero(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    stopped = False

    class RestoreFailure(SafetyAdapter):
        def write(self, name, value):
            if stopped and name == "done":
                raise RuntimeError("restore failed")
            super().write(name, value)

    adapter = RestoreFailure()

    def wait(_seconds):
        nonlocal stopped
        stopped = True
        return True

    code = run_simulator(
        SimulatorConfig(profile=profile),
        adapter_factory=lambda _config: adapter,
        stop_requested=lambda: stopped,
        interruptible_wait=wait,
    )
    assert code == 1
    assert adapter.closed


def test_first_write_rechecks_stop_and_deadline_after_original_read():
    stopped = False

    class StopAfterRead(SafetyAdapter):
        def read(self, name):
            nonlocal stopped
            value = super().read(name)
            stopped = True
            return value

    writer = TrackedWriter(StopAfterRead({"x": 1}), stop_requested=lambda: stopped)
    with pytest.raises(InterruptedError):
        writer.write("x", 2)

    expired = False

    class ExpireAfterRead(SafetyAdapter):
        def read(self, name):
            nonlocal expired
            value = super().read(name)
            expired = True
            return value

    writer = TrackedWriter(
        ExpireAfterRead({"x": 1}), deadline_exceeded=lambda: expired
    )
    with pytest.raises(GlobalTimeoutError):
        writer.write("x", 2)


def test_write_checks_stop_and_deadline_before_original_read():
    stopped_adapter = SafetyAdapter({"x": 1})
    with pytest.raises(InterruptedError):
        TrackedWriter(
            stopped_adapter,
            stop_requested=lambda: True,
        ).write("x", 2)
    assert stopped_adapter.reads == []

    expired_adapter = SafetyAdapter({"x": 1})
    with pytest.raises(GlobalTimeoutError):
        TrackedWriter(
            expired_adapter,
            deadline_exceeded=lambda: True,
        ).write("x", 2)
    assert expired_adapter.reads == []


def test_snapshot_reads_check_stop_before_every_io(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    stopped = False

    class StopDuringRead(SafetyAdapter):
        def read(self, name):
            nonlocal stopped
            value = super().read(name)
            stopped = True
            return value

    adapter = StopDuringRead()
    assert run_simulator(
        SimulatorConfig(profile=profile),
        adapter_factory=lambda _config: adapter,
        stop_requested=lambda: stopped,
    ) == 0
    assert len(adapter.reads) == 1
    assert adapter.writes == []
    assert adapter.closed


def test_stop_during_initial_writes_interrupts_remaining_runtime_io(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    stopped = False

    class StopOnFirstWrite(SafetyAdapter):
        def write(self, name, value):
            nonlocal stopped
            super().write(name, value)
            if not stopped:
                stopped = True

    adapter = StopOnFirstWrite()
    assert run_simulator(
        SimulatorConfig(profile=profile),
        adapter_factory=lambda _config: adapter,
        stop_requested=lambda: stopped,
    ) == 0
    assert adapter.closed


def test_runner_write_exception_still_restores_prior_writes_and_closes(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))

    class ExplodingWrite(SafetyAdapter):
        def write(self, name, value):
            if name == "ready":
                raise RuntimeError("write failed")
            super().write(name, value)

    adapter = ExplodingWrite({"done": 9})
    with pytest.raises(RuntimeError, match="write failed"):
        run_simulator(
            SimulatorConfig(profile=profile),
            adapter_factory=lambda _config: adapter,
        )
    assert adapter.values["done"] == 9
    assert adapter.closed


def test_every_later_write_rechecks_stop_and_deadline():
    adapter = SafetyAdapter({"x": 1})
    stopped = False
    writer = TrackedWriter(adapter, stop_requested=lambda: stopped)
    writer.write("x", 2)
    stopped = True
    with pytest.raises(InterruptedError):
        writer.write("x", 3)
    assert adapter.writes == [("x", 2)]

    adapter = SafetyAdapter({"x": 1})
    expired = False
    writer = TrackedWriter(adapter, deadline_exceeded=lambda: expired)
    writer.write("x", 2)
    expired = True
    with pytest.raises(GlobalTimeoutError):
        writer.write("x", 3)
    assert adapter.writes == [("x", 2)]


def test_adapter_forwards_io_and_propagates_read_write_errors():
    class Device:
        def __init__(self):
            self.closed = False

        def read_variable(self, name, use_cache):
            assert not use_cache
            if name == "bad":
                raise RuntimeError("read")
            return 7

        def write_variable(self, name, value):
            if name == "bad":
                raise RuntimeError("write")
            self.written = (name, value)

        def disconnect(self):
            self.closed = True

    device = Device()
    adapter = simulator.SZLabOpcAdapter(device)
    assert adapter.read("x") == 7
    assert adapter.read_many(["x", "y"]) == {"x": 7, "y": 7}
    adapter.write("x", 8)
    assert device.written == ("x", 8)
    with pytest.raises(RuntimeError, match="read"):
        adapter.read("bad")
    with pytest.raises(RuntimeError, match="write"):
        adapter.write("bad", 1)
    adapter.close()
    assert device.closed


def test_adapter_snapshot_checks_stop_and_deadline_before_each_read():
    class Device:
        def __init__(self):
            self.reads = []

        def read_variable(self, name, use_cache=False):
            self.reads.append(name)
            return False

    device = Device()
    adapter = simulator.SZLabOpcAdapter(device)
    stopped = False

    def deadline_exceeded():
        nonlocal stopped
        if device.reads:
            stopped = True
        return False

    result = adapter.read_snapshot(
        ["first", "second"],
        stop_requested=lambda: stopped,
        deadline_exceeded=deadline_exceeded,
    )
    assert result is None
    assert device.reads == ["first"]

    with pytest.raises(GlobalTimeoutError):
        adapter.read_snapshot(
            ["third"],
            stop_requested=lambda: False,
            deadline_exceeded=lambda: True,
        )
    assert device.reads == ["first"]


@pytest.mark.parametrize("option", ["--io-timeout", "--poll-interval"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_cli_rejects_nonfinite_or_out_of_bounds_timing(option, value):
    with pytest.raises(SystemExit):
        simulator.build_parser().parse_args([option, value])


@pytest.mark.parametrize("field", ["io_timeout", "poll_interval"])
@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
def test_config_rejects_nonfinite_or_out_of_bounds_timing(field, value):
    with pytest.raises(ValueError):
        SimulatorConfig(**{field: value})


@pytest.mark.parametrize("raw", ["-1", "nan", "inf", "-inf"])
def test_cli_rejects_invalid_optional_global_timeout(raw):
    with pytest.raises(SystemExit):
        simulator.build_parser().parse_args(["--timeout", raw])


def test_cli_zero_timeout_means_unbounded_runtime():
    assert simulator.build_parser().parse_args(["--timeout", "0"]).timeout == 0


def test_config_zero_or_omitted_timeout_is_unbounded():
    assert SimulatorConfig().timeout is None
    assert SimulatorConfig(timeout=0).timeout is None


def test_cli_and_config_reject_io_timeout_above_maximum():
    with pytest.raises(SystemExit):
        simulator.build_parser().parse_args(["--io-timeout", "61"])
    with pytest.raises(ValueError):
        SimulatorConfig(io_timeout=61)


def test_generic_falling_edge_and_no_reset_node_releases_channel(tmp_path):
    value = payload()
    first = value["nodes"][0]
    first["trigger"] = cond("command", True, "falling")
    first["reset_when"] = None
    first["after_reset"] = None
    profile = load_simulator_profile(profile_path(tmp_path, value))
    machine = TaskOpcStateMachine(profile=profile)
    high = {"command": True, "number": 7, "other_command": False}
    low = dict(high, command=False)
    machine.prime(high)
    assert ops(machine, low, 0) == [("ready", False), ("done", 0)]
    assert ops(machine, low, 2) == [("done", 7), ("ready", True)]
    ops(machine, high, 3)
    assert ops(machine, low, 4) == [("ready", False), ("done", 0)]


def shared_channel_payload():
    value = payload()
    value["nodes"][1]["channel"] = "a"
    return value


def test_shared_channel_releases_after_on_complete_not_after_reset_delay(tmp_path):
    """after_reset 的 delay 只推迟写回，不阻塞同 channel 的其它 workflow 节点。"""
    profile = load_simulator_profile(
        profile_path(tmp_path, shared_channel_payload())
    )
    machine = TaskOpcStateMachine(profile=profile)
    low = {"command": False, "number": 7, "other_command": False}
    machine.prime(low)

    assert ops(machine, dict(low, command=True), 0) == [
        ("ready", False),
        ("done", 0),
    ]
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        0.5,
    ) == []
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        2,
    ) == [
        ("done", 7),
        ("ready", True),
        ("other_done", False),
    ]
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        3,
    ) == [("done", 0)]
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        4,
    ) == [("other_done", True)]


def test_edge_partial_completes_when_level_becomes_true_while_edge_stays_high(
    tmp_path,
):
    value = shared_channel_payload()
    value["nodes"][1]["trigger"]["all"].append(
        {"variable": "number", "operator": "eq", "value": 9, "edge": "level"}
    )
    profile = load_simulator_profile(profile_path(tmp_path, value))
    machine = TaskOpcStateMachine(profile=profile)
    low = {"command": False, "number": 7, "other_command": False}
    machine.prime(low)
    ops(machine, dict(low, command=True), 0)

    assert ops(
        machine,
        {"command": False, "number": 0, "other_command": True},
        0.5,
    ) == []
    assert ops(
        machine,
        {"command": False, "number": 9, "other_command": True},
        0.7,
    ) == []
    assert ops(
        machine,
        {"command": False, "number": 9, "other_command": True},
        2,
    ) == [
        ("done", 7),
        ("ready", True),
        ("other_done", False),
    ]
    assert ops(
        machine,
        {"command": False, "number": 9, "other_command": True},
        3,
    ) == [("done", 0)]
    assert ops(
        machine,
        {"command": False, "number": 9, "other_command": True},
        4,
    ) == [("other_done", True)]


def test_partial_edge_is_cancelled_when_source_reverses_before_levels_match(
    tmp_path,
):
    value = shared_channel_payload()
    value["nodes"][1]["trigger"]["all"].append(
        {"variable": "number", "operator": "eq", "value": 9, "edge": "level"}
    )
    profile = load_simulator_profile(profile_path(tmp_path, value))
    machine = TaskOpcStateMachine(profile=profile)
    low = {"command": False, "number": 7, "other_command": False}
    machine.prime(low)
    ops(machine, dict(low, command=True), 0)
    ops(
        machine,
        {"command": False, "number": 0, "other_command": True},
        0.5,
    )
    ops(
        machine,
        {"command": False, "number": 9, "other_command": False},
        0.7,
    )

    assert ops(
        machine,
        {"command": False, "number": 9, "other_command": False},
        2,
    ) == [("done", 7), ("ready", True)]
    result = ops(
        machine,
        {"command": False, "number": 9, "other_command": False},
        3,
    )
    assert result == [("done", 0)]


def test_shared_edge_generation_cannot_trigger_different_level_sibling(tmp_path):
    value = payload()
    sibling = json.loads(json.dumps(value["nodes"][0]))
    sibling["workflow_node_id"] = "a-eight"
    sibling["task_template_ids"] = ["ta"]
    sibling["trigger"]["all"][1]["value"] = 8
    sibling["on_complete"]["writes"][0]["value"] = 8
    value["nodes"].insert(1, sibling)
    profile = load_simulator_profile(profile_path(tmp_path, value))
    machine = TaskOpcStateMachine(profile=profile)
    low = {"command": False, "number": 7, "other_command": False}
    machine.prime(low)

    assert ops(machine, dict(low, command=True), 0) == [
        ("ready", False),
        ("done", 0),
    ]
    ops(machine, {"command": True, "number": 8, "other_command": False}, 0.5)
    assert ops(
        machine,
        {"command": False, "number": 8, "other_command": False},
        2,
    ) == [("done", 7), ("ready", True)]
    result = ops(
        machine,
        {"command": False, "number": 8, "other_command": False},
        3,
    )
    assert result == [("done", 0)]


def test_no_after_reset_releases_immediately_for_latched_trigger(tmp_path):
    value = shared_channel_payload()
    value["nodes"][0]["after_reset"] = None
    profile = load_simulator_profile(profile_path(tmp_path, value))
    machine = TaskOpcStateMachine(profile=profile)
    low = {"command": False, "number": 7, "other_command": False}
    machine.prime(low)
    ops(machine, dict(low, command=True), 0)
    ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        0.5,
    )

    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        2,
    ) == [("done", 7), ("ready", True), ("other_done", False)]


def test_draft_profile_is_rejected_before_lock_or_adapter_io(tmp_path):
    value = payload()
    value["status"] = "draft"
    value["nodes"][0]["channel"] = ""
    profile = load_simulator_profile(profile_path(tmp_path, value))
    lock_calls = 0
    adapter_calls = 0

    def lock_factory(_url):
        nonlocal lock_calls
        lock_calls += 1
        raise AssertionError("draft must not lock")

    def adapter_factory(_config):
        nonlocal adapter_calls
        adapter_calls += 1
        raise AssertionError("draft must not connect")

    with pytest.raises(ValueError, match="draft"):
        run_simulator(
            SimulatorConfig(profile=profile),
            adapter_factory=adapter_factory,
            lock_factory=lock_factory,
        )
    assert lock_calls == 0
    assert adapter_calls == 0


class RecordingLock:
    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail

    def acquire(self):
        self.events.append("lock")
        if self.fail:
            raise RuntimeError("endpoint already locked")

    def release(self):
        self.events.append("unlock")


def test_endpoint_lock_is_acquired_before_connect_and_released_after_close(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    events = []

    class OrderedAdapter(SafetyAdapter):
        def __init__(self):
            events.append("connect")
            super().__init__()

        def close(self):
            events.append("close")
            super().close()

    code = run_simulator(
        SimulatorConfig(profile=profile),
        adapter_factory=lambda _config: OrderedAdapter(),
        lock_factory=lambda _url: RecordingLock(events),
        interruptible_wait=lambda _seconds: True,
    )
    assert code == 0
    assert events[0:2] == ["lock", "connect"]
    assert events[-2:] == ["close", "unlock"]


def test_lock_contention_fails_before_adapter_connect(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    adapter_calls = 0

    def adapter_factory(_config):
        nonlocal adapter_calls
        adapter_calls += 1
        return SafetyAdapter()

    with pytest.raises(RuntimeError, match="already locked"):
        run_simulator(
            SimulatorConfig(profile=profile),
            adapter_factory=adapter_factory,
            lock_factory=lambda _url: RecordingLock([], fail=True),
        )
    assert adapter_calls == 0


def test_real_endpoint_lock_normalizes_url_and_is_nonblocking():
    first = simulator.create_endpoint_lock("OPC.TCP://Example.COM:4840/")
    second = simulator.create_endpoint_lock("opc.tcp://example.com:4840")
    assert first.path == second.path
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="同机"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_endpoint_lock_fails_cleanly_when_fcntl_is_unavailable(monkeypatch):
    monkeypatch.setattr(simulator, "fcntl", None)
    lock = simulator.create_endpoint_lock(DEFAULT_URL)
    with pytest.raises(RuntimeError, match="macOS/Linux"):
        lock.acquire()


def level_trigger_payload():
    value = payload()
    node = value["nodes"][0]
    node["trigger"] = cond("command", True, "level")
    node["reset_when"] = None
    node["after_reset"] = None
    node["on_complete"]["delay"] = 0.1
    return value


def test_pure_level_true_at_prime_stays_disarmed_until_group_is_false(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, level_trigger_payload()))
    machine = TaskOpcStateMachine(profile=profile)
    high = {"command": True, "number": 7, "other_command": False}
    machine.prime(high)

    assert ops(machine, high, 0) == []
    assert ops(machine, high, 0.1) == []


def test_pure_level_false_rearm_then_true_executes(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, level_trigger_payload()))
    machine = TaskOpcStateMachine(profile=profile)
    high = {"command": True, "number": 7, "other_command": False}
    low = dict(high, command=False)
    machine.prime(high)

    assert ops(machine, high, 0) == []
    assert ops(machine, low, 0.1) == []
    assert ops(machine, high, 0.2) == [("ready", False), ("done", 0)]


def test_pure_level_trigger_latches_while_shared_channel_is_busy(tmp_path):
    value = payload()
    value["nodes"][1]["channel"] = "a"
    value["nodes"][1]["trigger"] = cond("other_command", True, "level")
    profile = load_simulator_profile(profile_path(tmp_path, value))
    machine = TaskOpcStateMachine(profile=profile)
    low = {"command": False, "number": 7, "other_command": False}
    machine.prime(low)

    ops(machine, dict(low, command=True), 0)
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        0.5,
    ) == []
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        2,
    ) == [
        ("done", 7),
        ("ready", True),
        ("other_done", False),
    ]
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        3,
    ) == [("done", 0)]
    assert ops(
        machine,
        {"command": False, "number": 7, "other_command": True},
        4,
    ) == [("other_done", True)]


def test_mixed_edge_trigger_still_primes_stale_high_without_firing(tmp_path):
    profile = load_simulator_profile(profile_path(tmp_path, payload()))
    machine = TaskOpcStateMachine(profile=profile)
    high = {"command": True, "number": 7, "other_command": False}
    machine.prime(high)
    assert ops(machine, high, 0) == []


def test_lock_directory_and_file_permissions_are_private(tmp_path):
    lock = simulator.EndpointInstanceLock(
        DEFAULT_URL,
        lock_dir=tmp_path / "private-locks",
    )
    assert stat.S_IMODE(lock.path.parent.stat().st_mode) == 0o700
    lock.acquire()
    try:
        mode = stat.S_IMODE(lock.path.stat().st_mode)
        assert mode & ~0o600 == 0
        assert stat.S_ISREG(lock.path.stat().st_mode)
        assert lock.path.stat().st_uid == simulator.os.getuid()
    finally:
        lock.release()
    assert lock._file is None


def test_lock_rejects_symlink_without_modifying_target(tmp_path):
    lock = simulator.EndpointInstanceLock(
        DEFAULT_URL,
        lock_dir=tmp_path / "private-locks",
    )
    target = tmp_path / "target"
    target.write_text("sentinel", encoding="utf-8")
    lock.path.symlink_to(target)

    with pytest.raises(RuntimeError, match="符号链接"):
        lock.acquire()
    assert target.read_text(encoding="utf-8") == "sentinel"
    assert lock._file is None


def test_lock_rejects_overpermissive_existing_file(tmp_path):
    lock = simulator.EndpointInstanceLock(
        DEFAULT_URL,
        lock_dir=tmp_path / "private-locks",
    )
    lock.path.touch(mode=0o600)
    lock.path.chmod(0o644)

    with pytest.raises(RuntimeError, match="权限"):
        lock.acquire()
    assert lock._file is None


def test_lock_does_not_truncate_existing_secure_file(tmp_path):
    lock = simulator.EndpointInstanceLock(
        DEFAULT_URL,
        lock_dir=tmp_path / "private-locks",
    )
    lock.path.write_text("sentinel", encoding="utf-8")
    lock.path.chmod(0o600)
    lock.acquire()
    lock.release()
    assert lock.path.read_text(encoding="utf-8") == "sentinel"


def test_readme_simulator_command_parses_and_documents_schema_v2():
    readme = (Path(__file__).parents[2] / "README_zh.md").read_text(encoding="utf-8")
    start = readme.index("python -m scripts.szlab_task_opc_simulator")
    end = readme.index("```", start)
    command = readme[start:end].replace("\\\n", " ")
    argv = shlex.split(command)[3:]
    simulator.build_parser().parse_args(argv)
    assert "--samples" not in command
    assert "schema v2" in readme
    assert "跨主机禁止并发" in readme
    assert "best-effort" in readme
    assert "ABA" in readme
