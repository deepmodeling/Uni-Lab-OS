"""Edge 内机构运行时：direct 执行、follow 跟随、参数换算、拒绝路径与观测流格式。"""
import json
import math
import time

import pytest

pytest.importorskip("pxr")
pytest.importorskip("scipy")

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from unilabos.runtime.observations import Descriptor  # noqa: E402
from unilabos.simulation.mechanism import MechanismError  # noqa: E402
from unilabos.simulation.mechanism_runtime import FEED_SCHEMA, SCHEMA, MechanismRuntime  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from mechanism_fixtures import make_package  # noqa: E402

DRAWER = "MotionJoints.drawer_joint"
ARM = "MotionJoints.arm_joint"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _composition(attachment):
    return {"attachment": attachment, "feedback": "kinematic", "completion": "feedback", "scheduler": "coupled"}


def _config(tmp_path, attachment="direct", **extra):
    return {"schema": SCHEMA, "feed": str(tmp_path / "feed.jsonl"), "rate_hz": 10, **extra, "devices": {"node_a": {
        "package": str(make_package(tmp_path)), "composition": _composition(attachment),
        "actions": {"VENDOR_Open": {"mechanism": "open_door"},
                    "VENDOR_Swing": {"mechanism": "swing", "args": {"angle_deg": "angleEnum*1"}}}}}}


def _rows(tmp_path):
    return [json.loads(line) for line in (tmp_path / "feed.jsonl").read_text(encoding="utf8").splitlines()]


def test_direct_runs_bound_action_over_time_and_writes_followable_feed(tmp_path):
    clock = FakeClock()
    runtime = MechanismRuntime(_config(tmp_path), clock=clock, sleep=clock.sleep)
    assert runtime.simulated("node_a") and not runtime.follows("node_a")
    assert runtime.has("node_a", "VENDOR_Open") and not runtime.has("node_a", "open_door")
    result = runtime.run("node_a", "VENDOR_Open", {"timeout_tcp": 5000})  # 驱动独有的参数不传给机构动作
    assert result["evidence"] == "kinematic" and result["confirmed_steps"] and result["composition_digest"] and result["state"] == {"door": "open"}
    assert clock.now == pytest.approx(1002.0)  # 动作声明的 2 s
    rows = _rows(tmp_path)
    header, samples = rows[0], rows[1:]
    assert header["schema"] == FEED_SCHEMA and header["devices"]["node_a"] == sorted(header["devices"]["node_a"])
    descriptor = Descriptor.model_validate(header["descriptors"]["node_a"])  # 后端 observation_feed 用同一模型校验
    assert descriptor.joints[DRAWER] == "drawer_joint" and descriptor.properties[DRAWER].upper == pytest.approx(0.3)
    values = [row["values"]["node_a"][DRAWER] for row in samples]
    assert values[0] == 0.0 and values[-1] == pytest.approx(0.3) and values == sorted(values)
    assert all(set(row["values"]["node_a"]) == set(header["devices"]["node_a"]) for row in samples)  # 每行给全属性
    runtime.close()


def test_argument_mapping_converts_driver_parameters(tmp_path):
    clock = FakeClock()
    runtime = MechanismRuntime(_config(tmp_path, time_scale=100.0), clock=clock, sleep=clock.sleep)
    result = runtime.run("node_a", "VENDOR_Swing", {"angleEnum": 90, "timeout_tcp": 1})
    assert result["positions_si"]["MotionJoints/arm_joint"] == pytest.approx(math.pi / 2)
    runtime.close()


def test_direct_rejection_raises_and_leaves_device_free_and_unmoved(tmp_path):
    clock = FakeClock()
    runtime = MechanismRuntime(_config(tmp_path), clock=clock, sleep=clock.sleep)
    with pytest.raises(MechanismError) as error:
        runtime.run("node_a", "VENDOR_Swing", {"angleEnum": 120})
    assert error.value.code == "target_out_of_range"
    assert len(_rows(tmp_path)) == 2  # 只有表头和初始行
    assert runtime.run("node_a", "VENDOR_Open")["state"] == {"door": "open"}  # 设备没有被占住
    runtime.close()


def test_follow_begins_on_start_and_completes_only_on_success(tmp_path):
    runtime = MechanismRuntime(_config(tmp_path, attachment="protocol", time_scale=1.0))
    assert runtime.follows("node_a") and not runtime.simulated("node_a")
    assert runtime.begin("job-1", "node_a", "VENDOR_Open") is True
    time.sleep(0.35)
    failed = runtime.finish("job-1", success=False)  # 真机报失败：停在当前位置，不写语义状态
    assert failed["completed"] is False and 0.0 < failed["positions_si"]["MotionJoints/drawer_joint"] < 0.3
    assert runtime.devices["node_a"].mechanism.state == {}
    assert runtime.begin("job-2", "node_a", "VENDOR_Open") is True
    done = runtime.finish("job-2", success=True)  # 真机先于标称时长完成：直接收到终态
    assert done["completed"] is True and done["positions_si"]["MotionJoints/drawer_joint"] == pytest.approx(0.3)
    assert runtime.devices["node_a"].mechanism.state == {"door": "open"}
    assert runtime.finish("job-unknown", success=True) is None
    runtime.close()


def test_follow_rejection_never_blocks_the_real_action(tmp_path):
    runtime = MechanismRuntime(_config(tmp_path, attachment="protocol"))
    assert runtime.begin("job-1", "node_a", "VENDOR_Swing", {"angleEnum": 120}) is False
    assert runtime.rejections[0]["code"] == "target_out_of_range"
    assert runtime.finish("job-1", success=True) is None
    assert runtime.begin("job-2", "node_a", "VENDOR_Open") is True
    assert runtime.begin("job-3", "node_a", "VENDOR_Open") is False  # 同一设备正在推演
    assert runtime.rejections[-1]["code"] == "device_busy"
    runtime.finish("job-2", success=True)
    runtime.close()


def test_config_must_bind_existing_mechanism_actions(tmp_path):
    config = _config(tmp_path)
    config["devices"]["node_a"]["actions"]["VENDOR_Missing"] = {"mechanism": "does_not_exist"}
    with pytest.raises(ValueError):
        MechanismRuntime(config)
    with pytest.raises(ValueError):
        MechanismRuntime(dict(_config(tmp_path), schema="other"))


def test_argument_templates_build_text_arguments_such_as_site_names():
    assert MechanismRuntime._argument("column_{posY:02d}/level_{posX}", {"posX": 2, "posY": 8}) == "column_08/level_2"
    assert MechanismRuntime._argument("angleEnum*2", {"angleEnum": 45}) == 90
    with pytest.raises(MechanismError):
        MechanismRuntime._argument("{missing}", {})


def test_protocol_action_expands_through_a_station_step_table(tmp_path):
    """协议级动作（驱动只有一个“跑某协议”的动作）由工站步骤表展开成几步基本机构动作，一次动作连续执行。"""
    config = _config(tmp_path, time_scale=100.0)
    table = tmp_path / "steps.json"
    table.write_text(json.dumps({
        "open-and-swing": [{"mechanism": "open_door"}, {"mechanism": "swing", "args": {"angle_deg": "volume*2"}}],
        "swing-too-far": [{"mechanism": "open_door"}, {"mechanism": "swing", "args": {"angle_deg": "720"}}],
    }), encoding="utf8")
    config["devices"]["node_a"]["actions"]["VENDOR_Run"] = {"table": str(table), "key": "{protocol}"}
    clock = FakeClock()
    runtime = MechanismRuntime(config, clock=clock, sleep=clock.sleep)
    result = runtime.run("node_a", "VENDOR_Run", {"protocol": "open-and-swing", "volume": 45, "position": 1})
    assert result["positions_si"]["MotionJoints/drawer_joint"] == pytest.approx(0.3)
    assert result["positions_si"]["MotionJoints/arm_joint"] == pytest.approx(math.pi / 2)
    assert result["state"]["door"] == "open"
    with pytest.raises(MechanismError) as error:
        runtime.run("node_a", "VENDOR_Run", {"protocol": "not-in-table"})
    assert error.value.code == "protocol_unknown"
    before = dict(runtime.devices["node_a"].mechanism.positions)
    with pytest.raises(MechanismError):  # 第二步越界：整条不执行
        runtime.run("node_a", "VENDOR_Run", {"protocol": "swing-too-far"})
    assert runtime.devices["node_a"].mechanism.positions == before
    runtime.close()


def test_nested_argument_templates_fill_resource_names():
    arguments = MechanismRuntime._argument(["plate_{position}/A1", "plate_{position}/B1", 3], {"position": 2})
    assert arguments == ["plate_2/A1", "plate_2/B1", 3]
    assert MechanismRuntime._argument({"names": ["tips/tip_07"]}, {}) == {"names": ["tips/tip_07"]}


def _locked_config(tmp_path, digest=None):
    """随领域包提交的写法：包写成资产锁的键，路径相对配置文件；缓存目录由环境给。"""
    import hashlib
    import shutil

    cache = tmp_path / "cache"
    package = cache / "04_设备" / "pkg__R1"
    shutil.copytree(make_package(tmp_path), package)
    manifest = package / "com.nvidia.simready.packaging.json"
    manifest.write_text('{"name": "pkg"}', encoding="utf8")
    station = tmp_path / "station" / "config"
    station.mkdir(parents=True)
    (station / "assets.lock.json").write_text(json.dumps({"schema": "unilab.asset-dependencies/1", "dependencies": [{
        "key": "vendor_door", "asset_id": "a", "library_path": "04_设备/pkg__R1",
        "package_digest": digest or hashlib.sha256(manifest.read_bytes()).hexdigest()}]}), encoding="utf8")
    (station / "steps.json").write_text(json.dumps({"p1": [{"mechanism": "open_door"}]}), encoding="utf8")
    config = {"schema": SCHEMA, "rate_hz": 10, "assets": {"lock": "assets.lock.json"}, "devices": {"node_a": {
        "asset": "vendor_door", "composition": _composition("direct"),
        "actions": {"VENDOR_Open": {"mechanism": "open_door"},
                    "VENDOR_Run": {"table": "steps.json", "key": "{protocol}"}}}}}
    path = station / "mechanism-runtime.json"
    path.write_text(json.dumps(config), encoding="utf8")
    return path, cache


def test_packaged_config_resolves_locked_asset_and_relative_paths(tmp_path):
    path, cache = _locked_config(tmp_path)
    clock = FakeClock()
    feed = tmp_path / "run" / "feed.jsonl"
    runtime = MechanismRuntime.from_file(path, clock=clock, sleep=clock.sleep,
                                         environ={"UNILABOS_ASSET_CACHE": str(cache), "UNILABOS_MECHANISM_FEED": str(feed)})
    assert runtime.run("node_a", "VENDOR_Run", {"protocol": "p1"})["state"] == {"door": "open"}
    runtime.close()
    assert feed.is_file()


def test_packaged_config_rejects_missing_cache_or_digest_drift(tmp_path):
    path, cache = _locked_config(tmp_path)
    with pytest.raises(ValueError, match="UNILABOS_ASSET_CACHE"):
        MechanismRuntime.from_file(path, environ={})
    path, cache = _locked_config(tmp_path / "drift", digest="0" * 64)
    with pytest.raises(ValueError, match="摘要不一致"):
        MechanismRuntime.from_file(path, environ={"UNILABOS_ASSET_CACHE": str(cache)})


def test_follow_behavior_error_of_any_kind_releases_the_device(tmp_path, monkeypatch):
    """行为源码抛任意异常（例如参数校验的 ValueError）时推演不开始，设备不能一直占着。"""
    runtime = MechanismRuntime(_config(tmp_path, attachment="protocol"))

    def broken(*args, **kwargs):
        raise ValueError("workTime 必须是非负分钟数")

    monkeypatch.setattr(runtime, "_plan", broken)
    assert runtime.begin("job-1", "node_a", "VENDOR_Open") is False
    assert runtime.rejections[-1]["reason"].startswith("workTime")
    monkeypatch.undo()
    assert runtime.begin("job-2", "node_a", "VENDOR_Open") is True
    runtime.finish("job-2", True)
    runtime.close()


def test_intermediate_frames_hold_values_and_any_stage_query_projects_first(tmp_path):
    """逐帧推演只记关节值；读回、库位位置等读 stage 的查询之前先补摆，结果与逐帧摆位一致。"""
    from unilabos.simulation.mechanism import Playback

    runtime = MechanismRuntime(_config(tmp_path))
    mechanism = runtime.devices["node_a"].mechanism
    plan = runtime._plan(runtime.devices["node_a"], "VENDOR_Open", {})
    playback = Playback(mechanism, plan)
    held = playback.at(plan.duration / 2, project=False)
    assert mechanism._stale and held[DRAWER.replace(".", "/")] > 0
    assert mechanism.read()[DRAWER.replace(".", "/")] == pytest.approx(held[DRAWER.replace(".", "/")], abs=1e-6)
    assert not mechanism._stale
    assert playback.finish()["state"] == {"door": "open"}


@pytest.mark.parametrize("change, message", [
    ({"feedback": "analytic"}, "不能作为机构动作的证据"),
    ({"feedback": "physics"}, "物理引擎"),
    ({"attachment": "follow"}, "composition 无效"),
    ({"scheduler": "legacy"}, "coupled"),
    ({"sources": {"joints": "x"}}, "source_ports_invalid"),
])
def test_only_compiled_compositions_this_runtime_implements_are_accepted(tmp_path, change, message):
    """装配只认 composition；本运行时没实现的反馈档、旧写法 follow、非 coupled 调度在启动前报错。"""
    config = _config(tmp_path)
    config["devices"]["node_a"]["composition"].update(change)
    with pytest.raises(ValueError, match=message):
        MechanismRuntime(config)


def test_old_attachment_field_and_direct_under_twin_are_rejected(tmp_path):
    config = _config(tmp_path)
    config["devices"]["node_a"]["attachment"] = "direct"
    with pytest.raises(ValueError, match="composition.attachment"):
        MechanismRuntime(config)
    with pytest.raises(ValueError, match="孪生"):
        MechanismRuntime(_config(tmp_path), environ={"UNILABOS_OBSERVATION_FEED_PROVIDER": "twin-feed"})
    MechanismRuntime(_config(tmp_path, attachment="protocol"), environ={"UNILABOS_OBSERVATION_FEED_PROVIDER": "twin-feed"}).close()


def test_kinematic_completion_requires_readback_at_every_confirmation_step(tmp_path, monkeypatch):
    """计时到了不算完成：确认步读回不到位时 direct 动作失败、不写语义状态。"""
    clock = FakeClock()
    runtime = MechanismRuntime(_config(tmp_path), clock=clock, sleep=clock.sleep)
    mechanism = runtime.devices["node_a"].mechanism
    real = mechanism.set_positions

    def stuck(values):   # 抽屉卡在 1 cm：stage 读回与计划不符
        readback = real(values)
        return {**readback, DRAWER.replace(".", "/"): 0.01}

    monkeypatch.setattr(mechanism, "set_positions", stuck)
    with pytest.raises(MechanismError) as error:
        runtime.run("node_a", "VENDOR_Open", {})
    assert error.value.code == "readback_mismatch" and mechanism.state.get("door") != "open"
    assert runtime.begin("job-1", "node_a", "VENDOR_Open") is True   # 失败后设备已释放
    runtime.close()


def test_advance_confirms_each_finished_segment_once(tmp_path):
    from unilabos.simulation.mechanism import Playback

    runtime = MechanismRuntime(_config(tmp_path))
    mechanism = runtime.devices["node_a"].mechanism
    plan = runtime._plan(runtime.devices["node_a"], "VENDOR_Open", {})
    playback = Playback(mechanism, plan)
    playback.advance(plan.duration / 2)
    assert playback.confirmed == [] and mechanism._stale
    playback.advance(plan.duration)
    assert [c["segment"] for c in playback.confirmed] == [0]
    assert playback.finish()["confirmed_steps"] == playback.confirmed and len(playback.confirmed) == 1
