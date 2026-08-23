import faulthandler
import json
import os
import platform
import shutil
import signal
import sys
from typing import Dict, Any, List
import networkx as nx
import yaml

# Windows 中文系统 stdout 默认 GBK，无法编码 banner / emoji 日志中的 Unicode 字符
# 强制 stdout/stderr 用 UTF-8，避免 print 触发 UnicodeEncodeError 导致进程崩溃
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

# 原生崩溃(段错误 / 0xC0000005 访问违例，常见于 C 扩展 import)发生时打印 Python 调用栈。
# 仅在致命信号(SIGSEGV/SIGABRT/SIGFPE 等)时触发，不影响 SIGINT/SIGTERM 的正常退出流程。
try:
    faulthandler.enable()
except (RuntimeError, ValueError, OSError):
    pass

# 首先添加项目根目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
unilabos_dir = os.path.dirname(os.path.dirname(current_dir))
if unilabos_dir not in sys.path:
    sys.path.append(unilabos_dir)

from unilabos.app.cli.parser import build_parser  # noqa: E402
from unilabos.app.cli.router import run_cli_command  # noqa: E402
from unilabos.utils.banner_print import print_status, print_unilab_banner  # noqa: E402
from unilabos.config.config import (  # noqa: E402
    BasicConfig,
    HTTPConfig,
    load_config,
    resolve_host_node_name,
)
from unilabos.utils.address import resolve_address  # noqa: E402

def load_config_from_file(config_path):
    if config_path is None:
        config_path = os.environ.get("UNILABOS_BASICCONFIG_CONFIG_PATH", None)
    if config_path:
        if not os.path.exists(config_path):
            print_status(f"配置文件 {config_path} 不存在", "error")
        elif not config_path.endswith(".py"):
            print_status(f"配置文件 {config_path} 不是Python文件，必须以.py结尾", "error")
        else:
            load_config(config_path)
    else:
        print_status(f"启动 UniLab-OS时，配置文件参数未正确传入 --config '{config_path}' 尝试本地配置...", "warning")
        load_config(config_path)


def _resolve_graph_file_path(file_path: str | None) -> str | None:
    if file_path is None:
        return None
    if os.path.isfile(file_path):
        return file_path
    temp_file_path = os.path.abspath(str(os.path.join(__file__, "..", "..", file_path)))
    if os.path.isfile(temp_file_path):
        print_status(f"使用相对路径{temp_file_path}", "info")
        return temp_file_path
    return file_path


def _load_graph_json_preview(file_path: str | None) -> Dict[str, Any] | None:
    if not file_path or not file_path.endswith(".json") or not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print_status(f"预读取 graph JSON 失败，跳过 community 包解析: {exc}", "warning")
        return None


def main():
    """主函数"""
    # CLI 仅负责解析和轻量子命令分流；设备 runtime 在后续按需启动。
    parser = build_parser()
    args = parser.parse_args()
    args_dict = vars(args)

    from unilabos.hostlink.startup import configure_heating_demo_args

    configure_heating_demo_args(args_dict)

    if run_cli_command(args, parser):
        return

    from unilabos.app.backend import (
        BackendConfigurationError,
        resolve_backend_selection,
    )

    try:
        backend_selection = resolve_backend_selection(
            args_dict["backend"],
            is_slave=args_dict.get("is_slave", False),
            visual=args_dict.get("visual", "disable"),
        )
    except BackendConfigurationError as exc:
        parser.error(str(exc))
    args_dict["backend"] = backend_selection.name
    if backend_selection.name == "ros2":
        # HostLink direct backend must not probe/import rclpy as a side effect.
        from unilabos.app.utils import patch_rclpy_dll_windows

        patch_rclpy_dll_windows()

    # 环境检查 - 检查并自动安装必需的包 (可选)
    skip_env_check = args_dict.get("skip_env_check", False)
    check_mode = args_dict.get("check_mode", False)

    if not skip_env_check:
        from unilabos.utils.environment_check import check_environment, check_device_package_requirements

        if not check_environment(auto_install=True):
            print_status("环境检查失败，程序退出", "error")
            os._exit(1)

        # 第一次设备包依赖检查：build_registry 之前，确保 import map 可用
        devices_dirs_for_req = args_dict.get("devices", None)
        if devices_dirs_for_req:
            if not check_device_package_requirements(devices_dirs_for_req):
                print_status("设备包依赖检查失败，程序退出", "error")
                os._exit(1)
    else:
        print_status("跳过环境依赖检查", "warning")

    # 加载配置文件，优先加载config，然后从env读取
    config_path = args_dict.get("config")

    # === 解析 working_dir ===
    # 规则1: working_dir 传入 → 检测 unilabos_data 子目录，已是则不修改
    # 规则2: 仅 config_path 传入 → 用其父目录作为 working_dir
    # 规则4: 两者都传入 → 各用各的，但 working_dir 仍做 unilabos_data 子目录检测
    raw_working_dir = args_dict.get("working_dir")
    if raw_working_dir:
        working_dir = os.path.abspath(raw_working_dir)
    elif config_path and os.path.exists(config_path):
        working_dir = os.path.dirname(os.path.abspath(config_path))
    else:
        working_dir = os.path.abspath(os.getcwd())

    # unilabos_data 子目录自动检测
    if os.path.basename(working_dir) != "unilabos_data":
        unilabos_data_sub = os.path.join(working_dir, "unilabos_data")
        if os.path.isdir(unilabos_data_sub):
            working_dir = unilabos_data_sub
        elif not raw_working_dir and not (config_path and os.path.exists(config_path)):
            # 未显式指定路径，默认使用 cwd/unilabos_data
            working_dir = os.path.abspath(os.path.join(os.getcwd(), "unilabos_data"))

    # === 解析 config_path ===
    if config_path and not os.path.exists(config_path):
        # config_path 传入但不存在，尝试在 working_dir 中查找
        candidate = os.path.join(working_dir, "local_config.py")
        if os.path.exists(candidate):
            config_path = candidate
            print_status(f"在工作目录中发现配置文件: {config_path}", "info")
        else:
            print_status(
                f"配置文件 {config_path} 不存在，工作目录 {working_dir} 中也未找到 local_config.py，"
                f"请通过 --config 传入 local_config.py 文件路径",
                "error",
            )
            os._exit(1)
    elif not config_path:
        # 规则3: 未传入 config_path，尝试 working_dir/local_config.py
        candidate = os.path.join(working_dir, "local_config.py")
        if os.path.exists(candidate):
            config_path = candidate
            print_status(f"发现本地配置文件: {config_path}", "info")
        elif args_dict.get("demo_mode", False):
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "config",
                "example_config.py",
            )
            print_status("演示模式使用内置默认配置，不创建 local_config.py", "info")
        else:
            print_status("未指定config路径，可通过 --config 传入 local_config.py 文件路径", "info")
            print_status(f"您是否为第一次使用？并将当前路径 {working_dir} 作为工作目录？ (Y/n)", "info")
            if check_mode or input() != "n":
                os.makedirs(working_dir, exist_ok=True)
                config_path = os.path.join(working_dir, "local_config.py")
                shutil.copy(
                    os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "example_config.py"),
                    config_path,
                )
                print_status(f"已创建 local_config.py 路径： {config_path}", "info")
            else:
                os._exit(1)

    # 加载配置文件 (check_mode 跳过)
    print_status(f"当前工作目录为 {working_dir}", "info")
    if not check_mode:
        load_config_from_file(config_path)

    # 根据配置重新设置日志级别
    from unilabos.utils.log import configure_logger, configure_comm_logger, logger

    if hasattr(BasicConfig, "log_level"):
        logger.info(f"Log level set to '{BasicConfig.log_level}' from config file.")
    file_path = configure_logger(loglevel=BasicConfig.log_level, working_dir=working_dir)
    if file_path is not None:
        logger.info(f"[LOG_FILE] {file_path}")

    # 为服务端通信(WebSocket)配置独立日志，避免与主日志混在一起，便于排查通信机制
    comm_log_path = configure_comm_logger(loglevel=BasicConfig.log_level, working_dir=working_dir)
    if comm_log_path is not None:
        logger.info(f"[COMM_LOG_FILE] {comm_log_path}")

    address = args_dict.get("address")
    if address:
        HTTPConfig.remote_addr = resolve_address(address)
        print_status(f"使用统一服务地址: {HTTPConfig.remote_addr}", "info")

    # 设置BasicConfig参数
    if args_dict.get("ak", ""):
        BasicConfig.ak = args_dict.get("ak", "")
        print_status("传入了ak参数，优先采用传入参数！", "info")
    if args_dict.get("sk", ""):
        BasicConfig.sk = args_dict.get("sk", "")
        print_status("传入了sk参数，优先采用传入参数！", "info")
    BasicConfig.working_dir = working_dir

    # ROS2 backend 用 HostLink 辅助发现；hostlink backend 则在同一 TCP 长连接上
    # 直接同步设备描述/状态和执行设备动作，不导入 ROS。
    is_slave = bool(args_dict.get("is_slave", False))
    from unilabos.hostlink.startup import (
        apply_hostlink_cli,
        validate_hostlink_backend,
    )

    try:
        apply_hostlink_cli(args_dict, is_slave=is_slave)
        validate_hostlink_backend(args_dict, is_slave=is_slave)
    except ValueError as exc:
        parser.error(str(exc))

    BasicConfig.port = (
        args_dict["port_management"]
        if args_dict["port_management"] is not None
        else BasicConfig.port
    )
    BasicConfig.disable_browser = args_dict["disable_browser"] or BasicConfig.disable_browser
    BasicConfig.is_host_mode = not is_slave
    BasicConfig.slave_no_host = args_dict.get("slave_no_host", False)
    BasicConfig.no_update_feedback = args_dict.get("no_update_feedback", False)
    BasicConfig.test_mode = args_dict.get("test_mode", False)
    BasicConfig.demo_mode = args_dict.get("demo_mode", False)
    if BasicConfig.test_mode:
        print_status("启用测试模式：所有动作将模拟执行，不调用真实硬件", "warning")
    BasicConfig.extra_resource = args_dict.get("extra_resource", False)
    if BasicConfig.extra_resource:
        print_status("启用额外资源加载：将加载lab_开头的labware资源定义", "info")
    BasicConfig.backend = args_dict["backend"]
    machine_name = platform.node()
    machine_name = "".join([c if c.isalnum() or c == "_" else "_" for c in machine_name])
    BasicConfig.machine_name = machine_name
    BasicConfig.vis_2d_enable = args_dict["2d_vis"]
    BasicConfig.check_mode = check_mode
    BasicConfig.host_node_name = resolve_host_node_name(
        args_dict.get("host_node_name") or BasicConfig.host_node_name
    )

    from unilabos.registry.registry import build_registry

    # 显示启动横幅
    print_unilab_banner(args_dict)

    # Step -1: 预读取 graph 中的 community.* class，并在 build_registry 前挂载社区设备包
    if not check_mode:
        graph_file_path = _resolve_graph_file_path(args_dict.get("graph") or BasicConfig.startup_json_path)
        args_dict["_graph_file_path"] = graph_file_path
        graph_preview = _load_graph_json_preview(graph_file_path)

        if graph_preview:
            from unilabos.app.community_packages import (
                CommunityPackageError,
                prepare_community_packages,
            )

            try:
                community_result = prepare_community_packages(
                    graph_preview,
                    working_dir=BasicConfig.working_dir,
                )
            except CommunityPackageError as exc:
                print_status(str(exc), "error")
                os._exit(1)

            if community_result.devices_dirs:
                existing_devices_dirs = args_dict.get("devices") or []
                args_dict["devices"] = existing_devices_dirs + community_result.devices_dirs
                if not skip_env_check:
                    from unilabos.utils.environment_check import (
                        check_device_package_requirements,
                        install_requirements_list,
                    )

                    # 社区包依赖：pyproject [project].dependencies 为标准来源，只装依赖不装包体
                    # （保持源码挂载，便于 track/卸载）；requirements.txt 作为补充兜底
                    if community_result.dependencies and not install_requirements_list(
                        community_result.dependencies, label="community"
                    ):
                        print_status("community 设备包 pyproject 依赖安装失败，程序退出", "error")
                        os._exit(1)
                    if not check_device_package_requirements(args_dict["devices"]):
                        print_status("community 设备包依赖检查失败，程序退出", "error")
                        os._exit(1)
            # 社区包设备直接以 community.<ns>.<id> 注册（扫描期命名空间化），不做 alias 桥接
            args_dict["_community_namespaces"] = community_result.namespaces

    # Step 0: AST 分析优先 + YAML 注册表加载
    # Host 的模板同步需要完整 config_info；check_mode 也执行实际 import 验证。
    devices_dirs = args_dict.get("devices", None)
    complete_registry = args_dict.get("complete_registry", False) or check_mode
    external_only = args_dict.get("external_devices_only", False)
    lab_registry = build_registry(
        registry_paths=args_dict["registry_path"],
        devices_dirs=devices_dirs,
        community_namespaces=args_dict.get("_community_namespaces"),
        upload_registry=BasicConfig.is_host_mode,
        check_mode=check_mode,
        complete_registry=complete_registry,
        external_only=external_only,
    )

    # Check mode: 注册表验证完成后直接退出
    if check_mode:
        device_count = len(lab_registry.device_type_registry)
        resource_count = len(lab_registry.resource_type_registry)
        print_status(f"Check mode: 注册表验证完成 ({device_count} 设备, {resource_count} 资源)，退出", "info")
        os._exit(0)

    # 以下导入依赖 ROS2 环境，check_mode 已退出不需要
    from unilabos.resources.graphio import (
        read_node_link_json,
        read_graphml,
        modify_to_backend_format,
    )
    from unilabos.server.backend import get_backend_client
    from unilabos.resources.resource_tracker import ResourceTreeSet, ResourceDict

    graph: nx.Graph
    resource_tree_set: ResourceTreeSet
    resource_links: List[Dict[str, Any]]

    file_path = args_dict.get("_graph_file_path")
    if file_path is None:
        file_path = _resolve_graph_file_path(args_dict.get("graph") or BasicConfig.startup_json_path)
    if file_path is None:
        print_status(
            "未指定设备加载文件；请使用 -g 指定本地图",
            "error",
        )
        os._exit(1)
    else:
        if file_path.endswith(".json"):
            graph, resource_tree_set, resource_links = read_node_link_json(file_path)
        else:
            graph, resource_tree_set, resource_links = read_graphml(file_path)
    import unilabos.resources.graphio as graph_res

    graph_res.physical_setup_graph = graph
    resource_edge_info = modify_to_backend_format(resource_links)
    materials = lab_registry.obtain_registry_resource_info()
    materials.extend(lab_registry.obtain_registry_device_info())
    materials = {k["id"]: k for k in materials}
    # 从 ResourceTreeSet 中获取节点信息
    nodes = {node.res_content.id: node.res_content for node in resource_tree_set.all_nodes}
    edge_info = len(resource_edge_info)
    for ind, i in enumerate(resource_edge_info[::-1]):
        source_node: ResourceDict = nodes[i["source"]]
        target_node: ResourceDict = nodes[i["target"]]
        if "sourceHandle" not in source_node:
            continue
        if "targetHandle" not in target_node:
            continue
        source_handle = i["sourceHandle"]
        target_handle = i["targetHandle"]
        source_handler_keys = [
            h["handler_key"] for h in materials[source_node.klass]["handles"] if h["io_type"] == "source"
        ]
        target_handler_keys = [
            h["handler_key"] for h in materials[target_node.klass]["handles"] if h["io_type"] == "target"
        ]
        if source_handle not in source_handler_keys:
            print_status(
                f"节点 {source_node.id} 的source端点 {source_handle} 不存在，请检查，支持的端点 {source_handler_keys}",
                "error",
            )
            resource_edge_info.pop(edge_info - ind - 1)
            continue
        if target_handle not in target_handler_keys:
            print_status(
                f"节点 {target_node.id} 的target端点 {target_handle} 不存在，请检查，支持的端点 {target_handler_keys}",
                "error",
            )
            resource_edge_info.pop(edge_info - ind - 1)
            continue

    # 使用 ResourceTreeSet 代替 list
    args_dict["resources_config"] = resource_tree_set
    args_dict["devices_config"] = resource_tree_set
    args_dict["graph"] = graph_res.physical_setup_graph

    if args_dict["controllers"] is not None:
        args_dict["controllers_config"] = yaml.safe_load(open(args_dict["controllers"], encoding="utf-8"))
    else:
        args_dict["controllers_config"] = None

    args_dict["bridges"] = []
    comm_client = None

    # Host 持有唯一微后端；Slave 只能经 HostLink 间接访问它。
    if BasicConfig.is_host_mode:
        comm_client = get_backend_client()
        args_dict["bridges"].append(comm_client)

        def _exit(signum, frame):
            comm_client.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, _exit)
        signal.signal(signal.SIGTERM, _exit)

        from unilabos.server.startup import setup_host_server_stack

        server_stack = setup_host_server_stack(
            args=args_dict,
            working_dir=working_dir,
            registry=lab_registry,
            communication_client=comm_client,
        )
        args_dict["bridges"].append(server_stack.execution_backend)
        print_status(
            f"微后端已启用: materials={server_stack.material_authority} "
            f"({server_stack.template_count} 个资源模板)",
            "info",
        )

        if server_stack.host_network is not None:
            from unilabos.config.config import HostLinkConfig

            print_status(
                "ROS2 HostLink 组网微后端已启用: "
                f"{HostLinkConfig.bind}:{server_stack.host_network.server.port}",
                "info",
            )

        # 微后端必须先于控制链路接收命令，避免首个 job_start 绕过生命周期权威。
        comm_client.start()
    else:
        print_status("SlaveMode跳过Websocket连接")
        if args_dict["backend"] == "ros2":
            # 正常 Slave 必须在 rclpy.init 前拿到 Host 的 ROS policy；
            # --slave_no_host 才允许离线启动并后台重连。
            from unilabos.server.scheduler.host_network import (
                require_slave_startup_device_ids,
                setup_slave_network_client,
            )

            setup_slave_network_client(
                device_ids=require_slave_startup_device_ids(
                    args_dict["devices_config"]
                )
            )

    args_dict["resources_mesh_config"] = {}
    args_dict["resources_edge_config"] = resource_edge_info
    from unilabos.app.runtime_startup import run_runtime

    try:
        run_runtime(args_dict)
    finally:
        if comm_client is not None:
            comm_client.stop()
        if BasicConfig.is_host_mode:
            from unilabos.server.scheduler.integration import shutdown_edge_services

            shutdown_edge_services()

if __name__ == "__main__":
    main()
