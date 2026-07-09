from __future__ import annotations

import atexit
import signal
import threading
from pathlib import Path

from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.sim.backends.factory import build_physics_backend
from unilabos.sim.backends.isaac.managed import ManagedIsaacWorker, ManagedIsaacWorkerConfig
from unilabos.sim.runtime import RuntimeServices, configure_runtime
from unilabos.utils import logger


_runtime_services: RuntimeServices | None = None
_managed_isaac_worker: ManagedIsaacWorker | None = None
_managed_isaac_shutdown_registered = False


def _stop_managed_isaac_worker() -> None:
    global _managed_isaac_worker
    if _managed_isaac_worker is not None:
        _managed_isaac_worker.stop()
        _managed_isaac_worker = None


def _register_managed_isaac_shutdown() -> None:
    global _managed_isaac_shutdown_registered
    if _managed_isaac_shutdown_registered:
        return
    atexit.register(_stop_managed_isaac_worker)
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handler = signal.getsignal(signum)

        def _handler(signum, frame, previous_handler=previous_handler):
            _stop_managed_isaac_worker()
            if callable(previous_handler):
                previous_handler(signum, frame)
                return
            if previous_handler == signal.SIG_IGN:
                return
            raise SystemExit(128 + signum)

        signal.signal(signum, _handler)
    _managed_isaac_shutdown_registered = True


def _start_managed_isaac_if_requested(kwargs: dict) -> ManagedIsaacWorker | None:
    global _managed_isaac_worker
    if not kwargs.get("isaac_managed", False):
        return None
    if kwargs.get("physics", "none") != "isaac":
        raise ValueError("--isaac_managed requires --physics isaac")
    if _managed_isaac_worker is not None:
        _stop_managed_isaac_worker()

    config = ManagedIsaacWorkerConfig(
        enabled=True,
        host=kwargs.get("isaac_host", "127.0.0.1"),
        port=int(kwargs.get("isaac_port", 8091)),
        scene=kwargs.get("physics_scene"),
        camera=kwargs.get("isaac_camera", "/World/Camera"),
        headless=bool(kwargs.get("isaac_headless", True)),
        joint_control_ui=bool(kwargs.get("isaac_joint_control_ui", False)),
        rpc_timeout_s=float(kwargs.get("isaac_rpc_timeout_s", 600.0)),
        start_timeout_s=float(kwargs.get("isaac_start_timeout", 120.0)),
        conda_env=kwargs.get("isaac_conda_env"),
        conda_executable=kwargs.get("isaac_conda_executable", "conda"),
        python_executable=kwargs.get("isaac_python", "python"),
        log_path=kwargs.get("isaac_log_path"),
        repo_root=Path.cwd(),
    )
    worker = ManagedIsaacWorker(config)
    worker.start()
    _managed_isaac_worker = worker
    _register_managed_isaac_shutdown()
    if not kwargs.get("physics_endpoint"):
        kwargs["physics_endpoint"] = worker.endpoint
    logger.info(f"Managed Isaac worker started at {worker.endpoint}")
    return worker


def _initialize_runtime_for_backend(backend: str, kwargs: dict) -> RuntimeServices:
    managed_worker = _start_managed_isaac_if_requested(kwargs)
    mode = kwargs.get("mode", "real")
    sim_rate = kwargs.get("sim_rate", 1.0)
    sim_paused = kwargs.get("sim_paused", False)
    physics_name = kwargs.get("physics", "none")
    physics_endpoint = kwargs.get("physics_endpoint")
    physics_scene = kwargs.get("physics_scene")
    physics_timeout = float(kwargs.get("physics_timeout", 120.0))
    try:
        physics = build_physics_backend(
            physics_name,
            endpoint=physics_endpoint,
            scene=physics_scene,
            timeout=physics_timeout,
        )
    except Exception:
        if managed_worker is not None:
            _stop_managed_isaac_worker()
        raise
    start_sim_services = backend == "ros" and not kwargs.get("disable_sim_services", False)
    services = configure_runtime(
        mode=mode,
        sim_rate=sim_rate,
        sim_paused=sim_paused,
        start_ros_services=False,
        physics=physics,
        physics_backend_name=physics_name,
        physics_endpoint=physics_endpoint,
        physics_scene=physics_scene,
        physics_timeout=physics_timeout,
    )
    services.context.sim_services_enabled = start_sim_services and mode in ("sim", "twin")
    services.context.query_api_enabled = backend == "ros" and not kwargs.get("disable_query_api", False)
    services.context.query_grpc_port = int(kwargs.get("query_grpc_port", 50051))
    services.context.query_labutopia_assets = kwargs.get("query_labutopia_assets")
    services.context.query_labutopia_config = kwargs.get("query_labutopia_config")
    services.context.query_labutopia_usd = kwargs.get("query_labutopia_usd")
    return services


# 根据选择的 backend 启动相应的功能
def start_backend(
    backend: str,
    devices_config: ResourceTreeSet,
    resources_config: ResourceTreeSet,
    resources_edge_config: list[dict] = [],
    graph=None,
    controllers_config: dict = {},
    bridges=[],
    is_slave: bool = False,
    visual: str = "None",
    resources_mesh_config: dict = {},
    **kwargs,
):
    global _runtime_services
    _runtime_services = _initialize_runtime_for_backend(backend, kwargs)
    logger.info(
        "Runtime mode initialized: "
        f"mode={_runtime_services.context.mode}, sim_rate={_runtime_services.context.clock.scale}, "
        f"paused={_runtime_services.context.clock.paused}, "
        f"sim_services={_runtime_services.context.sim_services_enabled}, "
        f"query_api={_runtime_services.context.query_api_enabled}, "
        f"grpc_port={_runtime_services.context.query_grpc_port}, "
        f"physics={_runtime_services.context.physics_backend_name}, "
        f"physics_endpoint={_runtime_services.context.physics_endpoint}, "
        f"physics_timeout={_runtime_services.context.physics_timeout}"
    )

    if backend == "ros":
        # 假设 ros_main, simple_main, automancer_main 是不同 backend 的启动函数
        from unilabos.ros.main_slave_run import main, slave  # 如果选择 'ros' 作为 backend
    elif backend == "simple":
        # 这里假设 simple_backend 和 automancer_backend 是你定义的其他两个后端
        # from simple_backend import main as simple_main
        pass
    elif backend == "automancer":
        # from automancer_backend import main as automancer_main
        pass
    else:
        raise ValueError(f"Unsupported backend: {backend}")

    backend_thread = threading.Thread(
        target=main if not is_slave else slave,
        args=(
            devices_config,
            resources_config,
            resources_edge_config,
            graph,
            controllers_config,
            bridges,
            visual,
            resources_mesh_config,
        ),
        name="backend_thread",
        daemon=True,
    )
    backend_thread.start()
    logger.info(f"Backend {backend} started.")
