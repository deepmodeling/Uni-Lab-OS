import faulthandler
import json
import os
import platform
import shutil
import signal
import sys
from pathlib import Path
from typing import Dict, Any, TYPE_CHECKING
import yaml

if TYPE_CHECKING:
    from unilabos.server.services.materials.graph import LoadedGraph

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
        print_status(f"启动 Uni-Lab-OS时，配置文件参数未正确传入 --config '{config_path}' 尝试本地配置...", "warning")
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


def _require_node_link_json(file_path: str) -> None:
    """``-g`` 只接受 node-link JSON；GraphML 等其它格式直接拒绝启动。"""

    if file_path.lower().endswith(".graphml"):
        print_status(
            f"-g {file_path}: 不再支持 GraphML 设备图。请把它转换为 node-link JSON"
            "（顶层 nodes + links，层级用 parent 表达，格式见 docs/user_guide/graph_files.md）"
            "后用 unilab -g <文件>.json 启动",
            "error",
        )
        os._exit(1)
    if os.path.isfile(file_path) and not file_path.endswith(".json"):
        print_status(
            f"-g {file_path}: 设备图文件必须是 node-link JSON（.json），"
            "格式见 docs/user_guide/graph_files.md",
            "error",
        )
        os._exit(1)


def _load_graph_json_preview(file_path: str | None) -> Dict[str, Any] | None:
    if not file_path or not file_path.endswith(".json") or not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print_status(f"预读取 graph JSON 失败，跳过 community 包解析: {exc}", "warning")
        return None


def _materialize_graph_from_authority(
    identity: str, args_dict: Dict[str, Any], working_dir: str
) -> str | None:
    """``-g <uuid|名称>``：从本机 Graph Authority 拉取图并落盘为缓存文件。

    图快照存于 materials.db 的 lab_graph 表；只读本机数据库（启动时微后端
    尚未监听 HTTP），云端图先用 ``unilab graph download --remote`` 下载为
    文件再启动。
    """

    paths = _own_database_paths(args_dict, working_dir)
    record = None
    if _graph_authority_is_remote():
        # 权威的 Host 子进程：Graph Authority 在权威的 materials.db，经 HTTP 读取
        from unilabos.client.materials.graph import HTTPGraphClient

        try:
            with HTTPGraphClient(HTTPConfig.remote_addr) as client:
                record = client.get_graph(identity)
        except Exception as exc:  # noqa: BLE001 - 读不到即按「不在权威中」处理
            print_status(f"从调度权威读取图 {identity} 失败: {exc}", "warning")
            return None
    else:
        if not os.path.isfile(paths.materials_db):
            return None

        from unilabos.server.services.materials.graph import GraphError, GraphService

        service = GraphService(paths.materials_db)
        try:
            try:
                record = service.get_graph(identity)
            except GraphError:
                return None
        finally:
            service.close()
    if not isinstance(record, dict) or not record.get("uuid"):
        return None

    cache_path = _write_graph_cache(paths, record["uuid"], record["payload"])
    print_status(
        f"已从本机 Graph Authority 加载图: {record['name']} "
        f"(uuid={record['uuid']}, revision={record['revision']})",
        "info",
    )
    return cache_path


def _own_database_paths(args_dict: Dict[str, Any], working_dir: str):
    """本进程自己的四库路径（``--role backend`` 落 ``<根目录>/backend``）；开库前先独占根目录。

    启动入口已经取过锁时 ``claim_database_root`` 返回同一把（进程内可重入）。
    """

    from unilabos.server.database import claim_database_root
    from unilabos.server.startup import process_database_root, resolve_database_paths

    root = process_database_root(args_dict, working_dir=working_dir)
    paths = resolve_database_paths(
        {**args_dict, "server_database_root": root}, working_dir=working_dir
    )
    claim_database_root(paths.root)
    return paths


def _graph_authority_is_remote() -> bool:
    """Graph Authority 是否在别的进程（本进程是调度权威拉起的 Host 子进程）。

    云端 ``--address`` 场景仍读本机库：云端图先 ``unilab graph download --remote``。
    """

    from unilabos.app.supervisor import is_host_child

    return is_host_child() and bool(str(HTTPConfig.remote_addr or "").strip())


def _write_graph_cache(paths, graph_uuid: str, payload: Dict[str, Any]) -> str:
    cache_dir = os.path.join(str(paths.root), "graph_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{graph_uuid}.json")
    with open(cache_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return cache_path


def _registry_device_site_templates() -> Dict[str, Any]:
    """从已构建的注册表提取 ``template_name -> available_sites`` 映射。"""

    from unilabos.resources.adapters.device_site import registry_device_site_templates

    return registry_device_site_templates()


def _read_graph_json(file_path: str) -> Dict[str, Any]:
    """读取 ``-g`` JSON 文件；读不出或不是对象即退出。"""

    try:
        with open(file_path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        print_status(f"读取 graph JSON 失败: {exc}", "error")
        os._exit(1)
    if not isinstance(payload, dict):
        print_status(f"graph JSON 必须是对象: {file_path}", "error")
        os._exit(1)
    return payload


def _register_graph_file_to_authority(
    file_path: str,
    args_dict: Dict[str, Any],
    working_dir: str,
    graph_payload: Dict[str, Any] | None = None,
) -> str:
    """``-g <文件>.json``：先登记到 Graph Authority，再以权威 payload 启动。

    与 ``materials.ensure`` 对启动物料的处理一致——本地文件只是创建入口，运行以
    权威为准：

    - 权威没有该图 → 整图创建：草稿节点 / Site 在此获得权威身份，设备节点的模板
      Site 一并实例化；
    - 权威已有该图 → 沿用权威快照，只把文件里权威没有的节点 / 连线补进来；文件对
      既有节点的修改（pose / config / 删除）不生效（改图请用前端或
      ``unilab graph upload``）；
    - 文件里的 uuid 与权威身份不一致、payload 非法 → 拒绝启动；
    - Graph Authority 不可达 → 拒绝启动：启动后一切都从权威拉，没有本地兜底。

    必须在 ``build_registry`` 之后调用。

    Args:
        graph_payload: 调用方已读取（并按需转换）的 payload；缺省时在此读取文件。

    Returns:
        权威 payload 的缓存文件路径。
    """

    from unilabos.server.database import DatabaseRootLockError
    from unilabos.server.services.materials.graph import GraphError, GraphService

    if graph_payload is None:
        graph_payload = _read_graph_json(file_path)

    graph_name = Path(str(file_path)).stem
    try:
        paths = _own_database_paths(args_dict, working_dir)
    except DatabaseRootLockError:
        raise
    except Exception as exc:
        print_status(f"Graph Authority 不可用，无法登记启动图: {exc}", "error")
        os._exit(1)

    if _graph_authority_is_remote():
        # 权威的 Host 子进程：经 HTTP 登记到权威的 Graph Authority；模板 Site 用本进程
        # （刚装的驱动包）注册表，权威注册表可能还没有这些设备类。
        from unilabos.client.materials.graph import HTTPGraphClient
        from unilabos.client.utils.envelope import EnvelopeError

        try:
            with HTTPGraphClient(HTTPConfig.remote_addr) as client:
                stored = client.upsert_graph(
                    name=graph_name,
                    payload=graph_payload,
                    device_site_templates=_registry_device_site_templates(),
                    on_existing="adopt",
                )
        except EnvelopeError as exc:
            print_status(f"启动图被 Graph Authority 拒绝 [{exc.code}]: {exc.error}", "error")
            os._exit(1)
        except Exception as exc:  # noqa: BLE001 - 权威不可达同样拒绝启动
            print_status(f"启动图登记 Graph Authority 失败，拒绝启动: {exc}", "error")
            os._exit(1)
    else:
        try:
            service = GraphService(paths.materials_db)
        except Exception as exc:
            print_status(f"Graph Authority 不可用，无法登记启动图: {exc}", "error")
            os._exit(1)
        try:
            try:
                stored = service.upsert_graph(
                    name=graph_name,
                    payload=graph_payload,
                    device_site_templates=_registry_device_site_templates(),
                    on_existing="adopt",
                )
            except GraphError as exc:
                print_status(
                    f"启动图被 Graph Authority 拒绝 [{exc.code}]: {exc.message}", "error"
                )
                os._exit(1)
            except Exception as exc:
                print_status(f"启动图登记 Graph Authority 失败，拒绝启动: {exc}", "error")
                os._exit(1)
        finally:
            service.close()

    _print_graph_registration(stored)
    return _write_graph_cache(paths, stored["uuid"], stored["payload"])


def _print_graph_registration(stored: Dict[str, Any]) -> None:
    summary = stored.get("summary") or {}
    identity = f"{stored['name']} (uuid={stored['uuid']}, revision={stored['revision']})"
    created = len(summary.get("created") or [])
    assigned = int(summary.get("uuid_assigned") or 0)
    if not summary.get("existing"):
        detail = f"节点 {created} 个"
        if assigned:
            detail += f"，发号 {assigned} 个身份"
        print_status(
            f"启动图已创建到 Graph Authority: {identity}，{detail}；"
            f"可用 unilab -g {stored['name']} 复用",
            "info",
        )
        return

    adopted = len(summary.get("adopted") or [])
    kept = summary.get("kept") or []
    detail = f"沿用权威节点 {adopted} 个"
    if created:
        detail += f"，文件新增 {created} 个已登记"
    if kept:
        detail += f"，权威独有 {len(kept)} 个（{', '.join(kept[:5])}{'…' if len(kept) > 5 else ''}）"
    print_status(
        f"启动图以 Graph Authority 为准: {identity}，{detail}；"
        "文件对既有节点的修改不生效，改图请用前端或 unilab graph upload",
        "info",
    )


def _load_startup_graph(
    args_dict: Dict[str, Any], working_dir: str, *, is_slave: bool
) -> "LoadedGraph":
    """Host 启动图：``-g`` → Graph Authority → 资源树 / backend 连线 / 物理拓扑图。

    本地 JSON 文件在读取边界由 legacy 适配层升级旧格式，再登记到 Graph Authority；
    之后一律以权威 payload（缓存文件）经 ``load_graph_payload`` 装配。不带 ``-g``
    时以空图启动（只有 host_node）。物理拓扑图同时交给协议编译器使用。
    """

    from unilabos.backend.runtime.workstation_protocol import set_physical_setup_graph
    from unilabos.server.services.materials.graph import load_graph_payload

    file_path = args_dict.get("_graph_file_path")
    if file_path is None:
        file_path = _resolve_graph_file_path(args_dict.get("graph") or BasicConfig.startup_json_path)
    if file_path is None:
        # 不带 -g：Host 以空图启动（只有 host_node），设备随后经驱动包 / 受管设备进程
        # 或其它 Slave 接入；Slave 没有设备就没有存在意义，仍然要求 -g。
        if is_slave:
            print_status("Slave 必须用 -g 指定要接入的设备图", "error")
            os._exit(1)
        print_status(
            "未指定 -g，以空图启动（仅 host_node）。设备可稍后从驱动包页安装并作为受管进程接入，"
            "或用 unilab -g <图文件.json> 重启。",
            "info",
        )
        payload: Dict[str, Any] = {"nodes": [], "links": []}
    else:
        _require_node_link_json(file_path)
        if not args_dict.get("_graph_from_authority") and os.path.isfile(file_path):
            # -g 本地 JSON 是创建入口：旧格式图在读取边界由 legacy 适配层转成
            # 当前契约，注册表就绪后登记到 Graph Authority（权威没有则创建，已有则
            # 以权威为准只补新增，冲突 / 不可达拒绝启动），再以权威 payload 装配。
            from unilabos.server.backend.legacy_adaptor.legacy.startup import (
                upgrade_startup_graph_payload,
            )

            graph_payload = upgrade_startup_graph_payload(_read_graph_json(file_path), file_path)
            file_path = _register_graph_file_to_authority(
                file_path, args_dict, working_dir, graph_payload
            )
            args_dict["_graph_file_path"] = file_path
            args_dict["_graph_from_authority"] = True
        payload = _read_graph_json(file_path)

    loaded = load_graph_payload(payload, machine_name=BasicConfig.machine_name or "本地")
    set_physical_setup_graph(loaded.physical_setup_graph)
    return loaded


def _first_run_confirmed(check_mode: bool) -> bool:
    """首次启动是否以当前路径作为工作目录（提示为 Y/n，默认「是」）。

    非交互启动（CI、nohup、服务管理器）没有标准输入，``input()`` 会抛 EOFError；
    此时按默认值处理，而不是让进程崩溃。只有明确输入 n / N 才放弃。
    """

    if check_mode:
        return True
    try:
        answer = input()
    except EOFError:
        return True
    return answer.strip().lower() != "n"


def _runs_local_authority(args_dict: Dict[str, Any]) -> bool:
    """本进程是否应作为「调度权威 + Host 子进程」的权威一侧运行（默认拓扑）。

    以下情况直接走单进程 Host：``--no-safe-restart``（调试：权威与 Host 同进程）、
    Slave、配置了 ``--address`` / ``remote_addr``（权威在别处），以及本进程本身就是
    权威拉起的 Host 子进程。
    """

    if args_dict.get("no_safe_restart") or args_dict.get("is_slave"):
        return False
    if str(HTTPConfig.remote_addr or "").strip():
        return False
    from unilabos.app.supervisor import is_host_child

    return not is_host_child()


#: 本进程是常驻运行时（调度权威 / Host / Slave / 监督进程），而不是一次性 CLI 子命令
_runtime_process = False


def main():
    """运行 Uni-Lab-OS CLI，并在需要时启动设备运行时。

    数据库无法迁移 / 迁移失败属于数据保护类错误：只给出可操作的说明并以退出码 1 结束，
    不打印调用栈，也不会继续用空库启动。数据库根目录已被另一个实例占用同样只打印说明，
    以退出码 73 结束（看护进程据此区分“根目录被占”与普通崩溃）。

    被本机父进程拉起的子进程（Host 子进程、薄监督下的 Host、受管 Slave）最先认领父进程：父进程
    无论怎么没了都随之有序退出（``app/lifetime.py``）。常驻运行时的有序收尾做完后立即结束进程，
    不再等解释器 join 线程池工作线程、析构全部对象——那些收尾可以把一次 SIGTERM 拖上几分钟。
    """
    global _runtime_process
    from unilabos.app import lifetime

    _runtime_process = False
    lifetime.follow_parent_from_env()
    try:
        _main_with_exit_codes()
    except BaseException as exc:
        if not _runtime_process:
            raise
        lifetime.exit_now(lifetime.exit_code_for(exc))
    if _runtime_process:
        lifetime.exit_now(0)


def _main_with_exit_codes() -> None:
    from unilabos.server.database import DatabaseRootLocked, DatabaseRootLockError
    from unilabos.server.database.schema import DatabaseMigrationFailed, DatabaseMigrationRequired

    try:
        _run_main()
    except (DatabaseMigrationRequired, DatabaseMigrationFailed) as exc:
        print_status(str(exc), "error")
        raise SystemExit(1) from None
    except DatabaseRootLockError as exc:
        from unilabos.app.supervisor import DATABASE_ROOT_LOCKED_EXIT_CODE

        print_status(str(exc), "error")
        raise SystemExit(
            DATABASE_ROOT_LOCKED_EXIT_CODE if isinstance(exc, DatabaseRootLocked) else 1
        ) from None


def _database_root_role(args_dict: Dict[str, Any]) -> str:
    """写进数据库根目录锁的进程角色，冲突提示里给第二个实例看。"""

    from unilabos.app.supervisor import is_host_child

    if args_dict.get("is_slave"):
        return "Slave"
    if args_dict.get("role") == "backend":
        return "调度权威（--role backend）"
    if is_host_child():
        return "Host 子进程"
    if str(HTTPConfig.remote_addr or "").strip():
        return "Host"
    if args_dict.get("no_safe_restart"):
        return "调度权威 + Host（--no-safe-restart 单进程）"
    return "调度权威"


def _claim_process_database_root(args_dict: Dict[str, Any], working_dir: str) -> None:
    """一个数据库根目录只归一个实例：本进程要打开的根目录在碰任何库之前先独占。

    - 默认 ``unilab``（权威）、``--address`` 顶层 Host、``--no-safe-restart``：``<根目录>``；
    - ``--role backend``：``<根目录>/backend``；
    - 权威拉起的 Host 子进程：命令行带的 ``<根目录>/edge``，与权威各持各的锁；
    - ``--is_slave``：Slave 把 ``-g`` 图登记进本机 Graph Authority（materials.db），同样要独占；
    - ``--check_mode`` 只校验注册表、不开库，不取锁。

    被看护的进程（Host 子进程、薄监督下的 Host、受管 Slave）重新拉起时上一代刚退出，给锁的
    释放留几秒；顶层启动不等，第二个实例立即失败。
    """

    if args_dict.get("check_mode"):
        return
    from unilabos.app.supervisor import RELAUNCH_ROOT_LOCK_WAIT_S, is_supervised
    from unilabos.server.database import claim_database_root
    from unilabos.server.startup import process_database_root

    lock = claim_database_root(
        process_database_root(args_dict, working_dir=working_dir),
        role=_database_root_role(args_dict),
        wait_s=RELAUNCH_ROOT_LOCK_WAIT_S if is_supervised() else 0.0,
    )
    print_status(f"数据库根目录 {lock.root}（本进程独占，锁文件 {lock.path.name}）", "info")


def _run_main():
    parser = build_parser()
    args = parser.parse_args()
    args_dict = vars(args)

    if run_cli_command(args, parser):
        return

    global _runtime_process
    _runtime_process = True

    from unilabos.app.supervisor import maybe_supervise

    maybe_supervise(args)

    from unilabos.server.backend.restart import set_safe_restart_enabled

    set_safe_restart_enabled(not bool(args_dict.get("no_safe_restart")))

    from unilabos.backend import (
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
    if backend_selection.name == "ros2" and args_dict.get("role") != "backend":
        import importlib.util

        if importlib.util.find_spec("rclpy") is None:
            parser.error(
                "当前为无 ROS 安装。默认请使用 --backend hostlink；"
                "仅需 ROS2 时安装 unilabos-ros2（jazzy/humble）及对应消息包。"
            )
        # HostLink direct backend must not probe/import rclpy as a side effect.
        from unilabos.app.utils import patch_rclpy_dll_windows

        patch_rclpy_dll_windows()

    # 环境检查 - 检查并自动安装必需的包 (可选)
    # backend 角色进程无 ROS/设备依赖，跳过环境自动安装
    skip_env_check = (
        args_dict.get("skip_env_check", False)
        or args_dict.get("role") == "backend"
    )
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
        # 显式请求（--skip_env_check / check_mode / backend 角色）的跳过是既定行为，不是告警
        print_status("按启动参数跳过环境依赖检查", "info")

    # 加载配置文件，优先加载config，然后从env读取
    config_path = args_dict.get("config")

    # === 解析 working_dir ===（规则见 unilabos/config/working_dir.py；默认 ~/.unilabos）
    from unilabos.config.working_dir import resolve_working_dir

    working_dir = resolve_working_dir(args_dict.get("working_dir"), config_path)

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
        else:
            print_status("未指定config路径，可通过 --config 传入 local_config.py 文件路径", "info")
            print_status(f"您是否为第一次使用？并将当前路径 {working_dir} 作为工作目录？ (Y/n)", "info")
            if _first_run_confirmed(check_mode):
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
    from unilabos.backend.hostlink.startup import (
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
    if BasicConfig.test_mode:
        print_status("启用测试模式：所有动作将模拟执行，不调用真实硬件", "warning")
    BasicConfig.extra_resource = args_dict.get("extra_resource", False)
    if BasicConfig.extra_resource:
        print_status("启用额外资源加载：将加载lab_开头的labware资源定义", "info")
    BasicConfig.backend = args_dict["backend"]
    # 分布式 slave 等场景通过环境变量显式指定身份；否则用主机名
    machine_name = (
        args_dict.get("machine_name")
        or os.environ.get("UNILABOS_BASICCONFIG_MACHINE_NAME")
        or (BasicConfig.machine_name if BasicConfig.machine_name != "undefined" else None)
        or platform.node()
    )
    machine_name = "".join([c if c.isalnum() or c == "_" else "_" for c in machine_name])
    BasicConfig.machine_name = machine_name
    BasicConfig.vis_2d_enable = args_dict["2d_vis"]
    BasicConfig.check_mode = check_mode
    BasicConfig.host_node_name = resolve_host_node_name(
        args_dict.get("host_node_name") or BasicConfig.host_node_name
    )

    # hostlink Host 在配置就绪后立刻打开 HostLink 端口，不等注册表、设备图与设备启动：
    # Slave 可以马上连上并登记，物料 / 设备请求在 Host 就绪前回「Host 启动中」由 Slave 重试。
    # 设备 runtime 启动时 HostLinkBackend 接管同一个 server。
    from unilabos.backend.hostlink.startup import (
        prebind_host_server,
        should_prebind_host_server,
    )

    if should_prebind_host_server(
        args_dict,
        is_slave=is_slave,
        check_mode=check_mode,
        runs_local_authority=_runs_local_authority(args_dict),
    ):
        prebind_host_server()

    from unilabos.registry.registry import build_registry

    # 显示启动横幅
    print_unilab_banner(args_dict)

    # 打开任何数据库之前先独占本进程的数据库根目录：第二个实例在这里就失败，不碰任何库
    _claim_process_database_root(args_dict, working_dir)

    # Step -1: 预读取 graph 中的 community.* class，并在 build_registry 前挂载社区设备包
    if not check_mode:
        graph_file_path = _resolve_graph_file_path(args_dict.get("graph") or BasicConfig.startup_json_path)
        if graph_file_path is not None:
            _require_node_link_json(graph_file_path)
        # 非文件参数按 uuid 或名称从本机 Graph Authority 解析。
        if graph_file_path is not None and not os.path.isfile(graph_file_path):
            materialized_path = _materialize_graph_from_authority(
                graph_file_path, args_dict, working_dir
            )
            if materialized_path is not None:
                graph_file_path = materialized_path
                args_dict["_graph_from_authority"] = True
            else:
                print_status(
                    f"-g {graph_file_path} 既不是本地文件，也不在本机 Graph Authority 中；"
                    "可先 unilab graph upload -f <文件>，或 unilab graph download --remote 拉取云端图",
                    "error",
                )
                os._exit(1)
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

    # 管理 API 装的驱动包（unilabos_data/driver_packages.json 中已启用者）并入扫描目录，
    # 安装后安静点重启即可加载，不用改启动命令。
    if not check_mode:
        from unilabos.server.services.driver_packages import enabled_package_dirs, get_driver_package_service

        if BasicConfig.is_host_mode and args_dict.get("role") != "backend" and not _runs_local_authority(args_dict):
            # 恢复在目录存在性判断之前完成，避免上次切换中断后遗漏已恢复的挂载目录。
            package_service = get_driver_package_service()
            pending_operations = package_service.mutation_gate.status()['unresolved_operations']
            if pending_operations:
                print_status(f"驱动包操作待核实，受管 slave 启动已阻止：{', '.join(pending_operations)}", "warning")

        ledger_dirs = [
            item
            for item in enabled_package_dirs(working_dir)
            if item not in (args_dict.get("devices") or [])
        ]
        if ledger_dirs:
            args_dict["devices"] = (args_dict.get("devices") or []) + ledger_dirs
            print_status(f"驱动包台账挂载目录: {', '.join(ledger_dirs)}", "info")

    # Step 0: AST 分析优先 + YAML 注册表加载
    # Host 的模板同步需要完整 config_info；check_mode 也执行实际 import 验证。
    devices_dirs = args_dict.get("devices", None)
    complete_registry = args_dict.get("complete_registry", False) or check_mode
    external_only = args_dict.get("external_devices_only", False)
    if not check_mode:
        from unilabos.server.services.driver_packages import get_driver_package_service

        if BasicConfig.is_host_mode and args_dict.get("role") != "backend" and not _runs_local_authority(args_dict):
            get_driver_package_service().configure(list(devices_dirs or []), bool(external_only), watch=True)
    lab_registry = build_registry(
        registry_paths=args_dict["registry_path"],
        devices_dirs=devices_dirs,
        community_namespaces=args_dict.get("_community_namespaces"),
        upload_registry=BasicConfig.is_host_mode,
        check_mode=check_mode,
        complete_registry=complete_registry,
        external_only=external_only,
    )

    # Check mode: 注册表验证完成后直接退出；扫描跳过的文件或导入失败的注册类都让校验失败（退出码 1）
    if check_mode:
        device_count = len(lab_registry.device_type_registry)
        resource_count = len(lab_registry.resource_type_registry)
        for skipped in lab_registry.verify_skipped:
            print_status(f"Check mode: 当前 backend 不支持，跳过导入校验: {skipped}", "warning")
        failures = lab_registry.check_failures()
        if failures:
            for failure in failures:
                print_status(f"Check mode: {failure}", "error")
            print_status(
                f"Check mode: 注册表校验失败 ({len(failures)} 个问题; {device_count} 设备, {resource_count} 资源)",
                "error",
            )
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)
        print_status(f"Check mode: 注册表验证完成 ({device_count} 设备, {resource_count} 资源)，退出", "info")
        # os._exit 不会刷新缓冲区：输出被管道接走时（CI）先刷出结论
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    # 调度权威进程：--role backend 只起权威（Edge 用 --address 接入）；默认拓扑则由
    # 权威持有管理端口并把 Host 作为子进程拉起，安静点重启只重启 Host。
    # 设备图与设备运行时都在 Host 进程里，下面的 Host 路径只在 Host 进程走。
    explicit_backend_role = args_dict.get("role") == "backend"
    if explicit_backend_role or _runs_local_authority(args_dict):
        from unilabos.app.backend_main import run_backend_process

        run_backend_process(
            args_dict, lab_registry, working_dir, launch_host=not explicit_backend_role
        )
        return

    # 以下导入依赖 ROS2 环境，check_mode 已退出不需要
    from unilabos.server.backend.legacy_adaptor import get_backend_client
    from unilabos.resources.resource_tracker import ResourceDict

    loaded_graph = _load_startup_graph(args_dict, working_dir, is_slave=is_slave)
    resource_tree_set = loaded_graph.resource_tree_set
    resource_edge_info = loaded_graph.links
    # 模板 handle 表只在校验图中的边时才需要；设备条目的投影要几百毫秒，空图 / 无边不算
    materials: Dict[str, Any] = {}
    if resource_edge_info:
        materials = {
            k["id"]: k
            for k in [
                *lab_registry.obtain_registry_resource_info(),
                *lab_registry.obtain_registry_device_info(),
            ]
        }
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
            h["handler_key"]
            for h in materials[source_node.template_name]["handles"]
            if h["io_type"] == "source"
        ]
        target_handler_keys = [
            h["handler_key"]
            for h in materials[target_node.template_name]["handles"]
            if h["io_type"] == "target"
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
    args_dict["graph"] = loaded_graph.physical_setup_graph

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

        # 处理器里要用的模块先导入好：信号可能恰好打断主线程里正在进行的导入
        from unilabos.app import lifetime
        from unilabos.server import lifecycle

        # 限时看门狗在装处理器之前启动：处理器里布置限时只做一次赋值，不拿锁、不起线程
        lifetime.start_exit_watchdog()
        from unilabos.server.backend.restart import (
            RESTART_EXIT_CODE,
            is_restart_requested,
        )

        def _exit(signum, frame):
            """停机信号：只在第一次时让主线程抛 SystemExit，沿 finally / atexit 有序收尾。

            处理器里不做阻塞动作（停控制 WS、关库都在 finally 里）。重复的信号——POSIX 上
            Ctrl+C 同时发给整个进程组、权威又转发一次 SIGTERM，rclpy 的处理器也会再转一次——
            一律忽略：再抛一次 SystemExit 只会打断正在进行的收尾。收尾整体限时
            ``lifetime.SHUTDOWN_DEADLINE_S``，超时强制结束，不会挂住不退。
            """

            code = RESTART_EXIT_CODE if is_restart_requested() else 0
            if not lifetime.begin_shutdown(code, reason=f"Host 收到停机信号 {signum}"):
                return
            # 唤醒在等「Host 路由挂好」/ SSE 心跳等的线程：它们若还在线程池里等着，会拖住退出
            lifecycle.begin_shutdown()
            sys.exit(code)

        signal.signal(signal.SIGINT, _exit)
        signal.signal(signal.SIGTERM, _exit)
        # 终端关闭（SIGHUP，薄监督进程也会转发）同样有序退出
        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            signal.signal(sighup, _exit)
        # 作为权威的 Host 子进程建在独立进程组，权威用 Ctrl+Break 请它退出；uvicorn 停机后
        # 会重新抛出该信号，没有处理器时 CRT 直接 _exit(3)，finally / atexit（关库、收受管
        # 设备进程）都不会跑。
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signal.signal(sigbreak, _exit)

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

        # 开机图物料权威对齐（与 Slave 的 materials.ensure 语义一致）：
        # 权威已有同 uuid 的物料则直接采用，没有则以图中 uuid 显式创建。
        if resource_tree_set.trees:
            from unilabos.protocol.materials import ACTOR_GRAPH
            from unilabos.resources import materials as materials_helper

            # 账本来源记为开机图；权威缓存文件名即图 uuid，本地文件则退化为文件名。
            graph_ref = (
                os.path.splitext(os.path.basename(str(file_path)))[0]
                if file_path
                else None
            )
            ensured = materials_helper.ensure(
                resource_tree_set,
                gateway=server_stack.materials_gateway,
                actor_type=ACTOR_GRAPH,
                actor_uuid=graph_ref,
            )
            print_status(
                f"开机物料权威对齐完成: {len(ensured.trees)} 棵树（uuid 与图一致）",
                "info",
            )

        # 图中的 links 在物料节点就绪后幂等写入 material_link。运行期间的
        # 拓扑变更继续写入该表，并由 /api/v1/graphs/live/payload 实时导出。
        if resource_edge_info:
            try:
                from unilabos.server.composition import get_server_services

                local_services = get_server_services()
                if local_services is not None:
                    link_stats = local_services.materials.ensure_links(
                        resource_edge_info
                    )
                    print_status(
                        "开机拓扑边对齐完成: "
                        f"新建 {link_stats['created']} / 更新 {link_stats['updated']} / "
                        f"不变 {link_stats['unchanged']} / 跳过 {link_stats['skipped']}",
                        "info",
                    )
            except Exception as exc:
                print_status(f"开机拓扑边对齐失败（不影响运行）: {exc}", "warning")

        # 启动时上报完整注册表快照给 Registry Authority：服务端按条目版本化变更，
        # 并挂起会影响活跃 workflow 的 action 变更。设备包里的 @workflow 是工作流
        # **模板**，随同一份快照上报（registry_type=workflow），由前端插入画布或经
        # POST /workflows/from-template 实例化，不再直接落成工作流。权威在远端
        # （--address）时走 HTTP；本机持有调度权威时直接写进程内服务，两条路径共用同一投影。
        registry_report = None
        if HTTPConfig.remote_addr:
            # 远端模式只接入 runtime.v1 微后端；注册表、物料、工作流和调度
            # 均由后端权威承接。旧云端字段兼容属于 Backend 侧
            # ``legacy_adaptor``，不能在 Edge 启动时分叉出第二套上联链路。
            from unilabos.server.backend.legacy_adaptor.sync.templates import (
                report_registry_snapshot,
            )

            registry_report = report_registry_snapshot(
                lab_registry, HTTPConfig.remote_addr
            )
        else:
            from unilabos.server.backend.legacy_adaptor.sync.templates import (
                report_registry_snapshot_local,
            )
            from unilabos.server.services.runtime.registry import get_registry_service

            local_registry_service = get_registry_service()
            if local_registry_service is not None:
                registry_report = report_registry_snapshot_local(
                    lab_registry,
                    local_registry_service,
                    edge_uuid=BasicConfig.machine_name or BasicConfig.host_node_name or "",
                )
        if registry_report is not None:
            counts = (registry_report.summary or {}).get("counts", {})
            print_status(
                f"注册表已上报: 设备 {registry_report.device_count} "
                f"资源 {registry_report.resource_count} "
                f"工作流模板 {registry_report.workflow_count}"
                + (
                    f"（新增 {counts.get('added', 0)} 更新 {counts.get('updated', 0)} "
                    f"挂起 {counts.get('pending', 0)} 移除 {counts.get('removed', 0)} "
                    f"不可用 {counts.get('unusable', 0)}）"
                    if counts
                    else "（服务端未返回条目级统计）"
                ),
                "info",
            )

        # 微后端必须先于控制链路接收命令，避免首个 job_start 绕过生命周期权威。
        comm_client.start()
    else:
        print_status("SlaveMode跳过Websocket连接")
        if args_dict["backend"] == "ros2":
            # 正常 Slave 必须在 rclpy.init 前拿到 Host 的 ROS policy；
            # --slave_no_host 才允许离线启动并后台重连。
            from unilabos.backend.hostlink.network import (
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
        from unilabos.app.lifetime import shutdown_started

        if shutdown_started():
            print_status("收到停机信号，正在有序退出（停控制链路、关库、停受管设备进程）", "info")
        if comm_client is not None:
            comm_client.stop()
        if BasicConfig.is_host_mode:
            from unilabos.server.backend.composition import shutdown_backend_services

            shutdown_backend_services()

    # 安静点重启：端口、数据库等均已在上方退出链路释放。监督进程看到约定
    # 退出码后用相同参数拉起新实例；--no-safe-restart 时只退出。
    from unilabos.server.backend.restart import exit_if_restart_requested

    exit_if_restart_requested()


if __name__ == "__main__":
    main()
