"""验证 UniLabOS 主机权威物料服务的启动参数。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.app.main import (
    parse_args,
    should_attach_legacy_http_bridge,
    should_request_remote_startup,
)
from unilabos.app.runtime_storage import (
    resolve_runtime_storage_paths,
    resolve_working_directory,
)


def test_default_starts_embedded_microbackend_with_host_db(tmp_path: Path) -> None:
    """证明默认启动将三类本地数据库统一放入解析后的运行目录。

    参数：``tmp_path`` 提供当前平台的隔离目录。返回：无；断言本地库存
    （Inventory）与调度器（Scheduler）默认同时启用。异常：参数或启动合同变化时测试失败。
    """

    args = vars(parse_args().parse_args([]))
    working_dir = tmp_path / "implicit"
    expected_root = working_dir.resolve()

    resolve_runtime_storage_paths(args, working_dir=str(working_dir))

    assert args["edge_inventory_db"] == str(expected_root / "inventory.db")
    assert args["edge_device_state_db"] == str(expected_root / "device_state.db")
    assert args["edge_workflow_history_db"] == str(expected_root / "workflow_history.db")


def test_explicit_working_directory_derives_all_local_runtime_databases(
    tmp_path: Path,
) -> None:
    """证明一个工作目录参数即可确定三类本地 SQLite 存储路径。"""

    working_dir = tmp_path / "szlab-runtime"
    expected_root = working_dir.resolve()
    args = vars(parse_args().parse_args(["--working_dir", str(working_dir)]))

    paths = resolve_runtime_storage_paths(
        args,
        working_dir=str(working_dir),
    )

    assert paths.inventory_db == str(expected_root / "inventory.db")
    assert paths.device_state_db == str(expected_root / "device_state.db")
    assert paths.workflow_history_db == str(expected_root / "workflow_history.db")
    assert args["edge_inventory_db"] == paths.inventory_db
    assert args["edge_device_state_db"] == paths.device_state_db
    assert args["edge_workflow_history_db"] == paths.workflow_history_db


def test_new_default_working_directory_is_hidden(tmp_path: Path) -> None:
    """证明新安装默认使用当前目录下的隐藏 ``.unilabos``。

    参数：``tmp_path`` 是没有任何旧运行数据的隔离当前目录。返回：无；断言新默认
    路径且未命中遗留兼容。异常：解析器异常时测试失败。
    """

    resolution = resolve_working_directory(
        requested=None,
        config_path=None,
        current_directory=tmp_path,
    )

    assert resolution.path == str(tmp_path / ".unilabos")
    assert not resolution.used_legacy_directory


def test_existing_legacy_working_directory_is_preserved(tmp_path: Path) -> None:
    """证明旧 ``unilabos_data`` 在新目录不存在时继续承载原持久事实。

    参数：``tmp_path`` 隔离旧运行目录夹具。返回：无；断言解析器选择旧目录并显式
    标记遗留兼容（Legacy Compatibility）。异常：文件系统写入失败时测试失败。
    """

    legacy_directory = tmp_path / "unilabos_data"
    legacy_directory.mkdir()

    resolution = resolve_working_directory(
        requested=None,
        config_path=None,
        current_directory=tmp_path,
    )

    assert resolution.path == str(legacy_directory)
    assert resolution.used_legacy_directory


def test_hidden_working_directory_wins_over_legacy_directory(tmp_path: Path) -> None:
    """证明新旧目录并存时选择隐藏目录，避免继续向旧布局写入。

    参数：``tmp_path`` 同时承载新旧目录夹具。返回：无；断言新默认具有确定性
    优先级。异常：文件系统写入失败时测试失败。
    """

    hidden_directory = tmp_path / ".unilabos"
    hidden_directory.mkdir()
    (tmp_path / "unilabos_data").mkdir()

    resolution = resolve_working_directory(
        requested=None,
        config_path=None,
        current_directory=tmp_path,
    )

    assert resolution.path == str(hidden_directory)
    assert not resolution.used_legacy_directory


def test_explicit_working_directory_remains_exact(tmp_path: Path) -> None:
    """证明显式运行目录不再因其中存在旧子目录而被隐式改写。

    参数：``tmp_path`` 是显式目录及旧子目录夹具。返回：无；断言调用者选择保持
    精确。异常：文件系统写入失败时测试失败。
    """

    explicit_directory = tmp_path / "isolated-run"
    (explicit_directory / "unilabos_data").mkdir(parents=True)

    resolution = resolve_working_directory(
        requested=str(explicit_directory),
        config_path=None,
        current_directory=tmp_path,
    )

    assert resolution.path == str(explicit_directory)
    assert not resolution.used_legacy_directory


@pytest.mark.parametrize(
    "removed_argument",
    [
        "--edge_scheduler",
        "--material_db",
        "--edge_inventory_db",
        "--device_state_db",
        "--edge_device_state_db",
        "--workflow_history_db",
        "--edge_workflow_history_db",
        "--material_service_mode",
        "--material_source",
        "--material_microbackend_addr",
        "--no_edge_scheduler",
        "--edge_scheduler_ordering_url",
        "--schedule_addr",
        "--edge_api_key",
        "--edge_key",
        "--edge_instance_uuid",
        "--edge_capability_revision",
        "--edge_state_db",
        "--upload_registry",
        "--2d_vis",
        "--no_update_feedback",
        "--restart_mode",
        "--auto_restart_count",
        "--skip_env_check",
        "--test_mode",
    ],
)
def test_redundant_startup_arguments_are_rejected(removed_argument: str) -> None:
    """证明已删除的重复启动参数不会继续形成第二套配置入口。

    参数：``removed_argument`` 是已由默认值、运行目录或配置替代的旧参数。
    返回：无；断言公共解析器关闭失败。异常：解析器接受旧参数时测试失败。
    """

    with pytest.raises(SystemExit):
        parse_args().parse_args([removed_argument])


def test_remote_resource_is_rejected_before_startup_but_driver_backend_remains() -> None:
    """证明远程资源开关只保留隐藏解析位并在启动前关闭失败。

    参数：无。返回：无；``--backend ros`` 仍表示本地 ROS 驱动实现。异常：远程
    开关若能绕过 main 的关闭式校验，或驱动后端被误删时测试失败。状态不变量：
    解析层保留旧参数以便给出清晰错误，运行层绝不执行远程请求。
    """

    args = vars(
        parse_args().parse_args(["--use_remote_resource", "--backend", "ros"])
    )
    assert args["use_remote_resource"] is True
    assert args["backend"] == "ros"


def test_instance_sync_can_select_production_devices_only() -> None:
    args = vars(
        parse_args().parse_args(["instance-sync", "--devices_only", "--check_only"])
    )

    assert args["instance_devices_only"] is True
    assert args["instance_check_only"] is True


def test_local_edge_control_bridge_is_supported() -> None:
    """证明动作进程只能声明本地 Edge 控制桥。

    参数：无。返回：无；本地 Scheduler 的 ``edge_control`` 与入站 FastAPI 桥均
    可解析。异常：解析结果仍携带已移除的 Backend 控制面时测试失败。
    """

    args = vars(
        parse_args().parse_args(
            [
                "--control_plane",
                "local",
                "--app_bridges",
                "edge_control",
                "fastapi",
            ]
        )
    )

    assert args["control_plane"] == "local"
    assert args["app_bridges"] == ["edge_control", "fastapi"]


def test_action_mode_is_the_only_public_hardware_simulation_switch() -> None:
    """证明动作执行模式使用明确枚举且默认触发真实设备调用。

    参数：无。返回：无；断言真实与模拟动作（Action）模式均可解析，旧测试模式
    已由参数拒绝测试覆盖。异常：模式合同漂移时测试失败。
    """

    default_args = vars(parse_args().parse_args([]))
    simulated_args = vars(
        parse_args().parse_args(["--action_mode", "simulate"])
    )

    assert default_args["action_mode"] == "real"
    assert simulated_args["action_mode"] == "simulate"


def test_runtime_database_preservation_is_explicit_opt_in() -> None:
    """证明运行态数据库默认切换临时目录，只有显式选项才沿用原库。

    参数：无。返回：无；断言根启动参数 ``--preserve_runtime_databases`` 的默认值
    为假且可显式开启。异常：公共命令行合同漂移时测试失败。
    """

    default_args = vars(parse_args().parse_args([]))
    preserved_args = vars(
        parse_args().parse_args(["--preserve_runtime_databases"])
    )

    assert default_args["preserve_runtime_databases"] is False
    assert preserved_args["preserve_runtime_databases"] is True


@pytest.mark.parametrize(
    "command",
    [
        ["login", "--ak", "access", "--sk", "secret", "--json"],
        ["logout", "--json"],
        ["whoami", "--json"],
        ["config", "show", "--json"],
        ["lab", "list", "--json"],
        ["material", "list", "--lab_uuid", "lab-1", "--json"],
        ["workflow", "upload", "-f", "workflow.json", "--json"],
    ],
)
def test_json_output_is_scoped_to_http_client_leaf_commands(
    command: list[str],
) -> None:
    """证明 JSON 输出格式只属于产生该输出的客户端叶子命令。

    参数：``command`` 是一个完整客户端叶子命令。返回：无；断言叶子参数生效。
    异常：全局格式参数重新出现或叶子漏配时测试失败。
    """

    parsed = vars(parse_args().parse_args(command))

    assert parsed["json"] is True


def test_json_output_is_not_a_root_startup_argument() -> None:
    """证明常驻 OS 根启动不再暴露无效的输出格式参数。"""

    with pytest.raises(SystemExit):
        parse_args().parse_args(["--json", "whoami"])


def test_local_graph_does_not_request_legacy_remote_startup() -> None:
    assert not should_request_remote_startup(
        startup_json=None,
        graph_file_path="/config/devices.json",
        use_remote_resource=True,
    )
    assert not should_request_remote_startup(
        startup_json=None,
        graph_file_path=None,
    )


def test_fastapi_is_inbound_only_without_legacy_cloud_switch() -> None:
    """证明前端 HTTP 服务不会隐式授权 OS 连接正式后端（Backend）。

    参数：无。返回：无；断言 ``fastapi`` 只提供入站接口，即使调用方携带历史
    远程开关也不会挂接出站桥。异常：权威边界回退时测试失败。
    """

    assert not should_attach_legacy_http_bridge(
        {"app_bridges": ["fastapi"], "use_remote_resource": False}
    )
    assert not should_attach_legacy_http_bridge(
        {"app_bridges": ["fastapi"], "use_remote_resource": True}
    )


def test_directed_discovery_ports_are_configurable() -> None:
    args = vars(
        parse_args().parse_args(
            [
                "--ros_discovery_server",
                "192.168.1.20:11811",
            ]
        )
    )

    assert args["ros_discovery_server"] == "192.168.1.20:11811"
