"""Edge microbackend ownership of Host/Slave networking.

The microbackend owns the HostLink listener, slave connection lifecycle, ROS
network configuration distribution and material-query dispatch.  ROS HostNode
only attaches its live resource tree after construction; it never binds the
HostLink port or builds the network policy itself.
"""

from __future__ import annotations

import atexit
import os
import threading
from typing import Any, Callable, Iterable, Optional, Tuple

from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink.client import (
    HostLinkClient,
    get_hostlink_client,
    set_hostlink_client,
)
from unilabos.hostlink.protocol import ActionType, PROTOCOL_VERSION
from unilabos.hostlink.resolver import LocalResourceResolver
from unilabos.hostlink.ros_assist import (
    FastDDSDiscoveryServer,
    RosNetworkInfo,
    apply_ros_network_env,
    available_udp_port,
    build_host_ros_info,
    detect_local_ip,
    format_host_port,
    parse_host_port,
    use_connected_host,
)
from unilabos.hostlink.server import HostLinkServer, set_hostlink_server
from unilabos.utils import logger

ResourceTreeGetter = Callable[[], Any]
SERVICE_OWNER = "edge-microbackend"


class HostNetworkService:
    """Host-side microbackend service for every connected slave."""

    def __init__(
        self,
        server: HostLinkServer,
        ros_info: RosNetworkInfo,
        resource_tree_getter: Optional[ResourceTreeGetter] = None,
        fallback_discovery_range: str = "",
    ) -> None:
        self.server = server
        self.ros_info = ros_info
        self._resource_tree_getter = resource_tree_getter
        self._resource_lock = threading.Lock()
        self._resolver = LocalResourceResolver(self._resource_tree)
        self._discovery_server: Optional[FastDDSDiscoveryServer] = None
        self._fallback_discovery_range = fallback_discovery_range

        self._refresh_hello_payload()
        self.server.register_handler(ActionType.MATERIAL, self._material)
        self.server.register_handler(ActionType.ROS_INFO, self._ros_info)

    def _refresh_hello_payload(self) -> None:
        self.server.hello_payload = {
            "host_id": BasicConfig.machine_name,
            "host_name": BasicConfig.machine_name,
            "owner": SERVICE_OWNER,
            "protocol_version": PROTOCOL_VERSION,
            "ros": self.ros_info.to_dict(),
        }

    @classmethod
    def from_config(
        cls,
        resource_tree_getter: Optional[ResourceTreeGetter] = None,
    ) -> "HostNetworkService":
        host_ip = HostLinkConfig.advertise_ip or detect_local_ip() or "127.0.0.1"
        domain_raw = str(HostLinkConfig.ros_domain_id or "").strip()
        static_peers = [
            peer.strip()
            for peer in HostLinkConfig.ros_static_peers.split(";")
            if peer.strip()
        ]
        configured_server = str(HostLinkConfig.ros_discovery_server or "").strip()
        managed_discovery = not configured_server
        discovery_disabled = False
        if configured_server.lower() in {"off", "none", "disabled"}:
            discovery_endpoint = ""
            managed_discovery = False
            discovery_disabled = True
        elif configured_server:
            # Fail early on malformed endpoints; silently advertising one would
            # isolate every Slave from the ROS graph.
            server_host, server_port = parse_host_port(configured_server)
            discovery_endpoint = format_host_port(server_host, server_port)
        else:
            configured_port = int(HostLinkConfig.ros_discovery_port or 0)
            discovery_port = configured_port or int(HostLinkConfig.port or 0)
            if discovery_port <= 0:
                discovery_port = available_udp_port(
                    HostLinkConfig.bind
                    if HostLinkConfig.bind not in ("", "::")
                    else "0.0.0.0"
                )
            discovery_endpoint = format_host_port(host_ip, discovery_port)

        ros_info = build_host_ros_info(
            host_ip=host_ip,
            domain_id=int(domain_raw) if domain_raw.isdigit() else None,
            discovery_range=HostLinkConfig.ros_discovery_range.strip().upper(),
            static_peers=static_peers or None,
            discovery_server=discovery_endpoint,
            discovery_server_managed=managed_discovery,
            discovery_server_disabled=discovery_disabled,
        )
        fallback_discovery_range = ros_info.automatic_discovery_range
        if discovery_endpoint:
            # Discovery Server and the generic Iron+ discovery controls must
            # not both rewrite participant discovery policy.
            ros_info.automatic_discovery_range = "SYSTEM_DEFAULT"
        service = cls(
            HostLinkServer(
                bind=HostLinkConfig.bind,
                port=HostLinkConfig.port,
                heartbeat_timeout=HostLinkConfig.heartbeat_timeout,
            ),
            ros_info,
            resource_tree_getter,
            fallback_discovery_range,
        )
        if managed_discovery:
            _host, discovery_port = parse_host_port(discovery_endpoint)
            service._discovery_server = FastDDSDiscoveryServer(
                HostLinkConfig.bind,
                discovery_port,
            )
        return service

    def start(self) -> "HostNetworkService":
        if self._discovery_server is not None:
            try:
                self._discovery_server.start()
                logger.info(
                    "[EdgeMicrobackend] Fast DDS directed discovery listening on UDP %s",
                    self._discovery_server.port,
                )
            except Exception as exc:  # noqa: BLE001 - retain legacy ROS discovery
                logger.error(
                    "[EdgeMicrobackend] directed DDS discovery unavailable; "
                    "falling back to the existing ROS discovery policy: %s",
                    exc,
                )
                self._discovery_server = None
                self.ros_info.discovery_server = ""
                self.ros_info.discovery_server_managed = False
                self.ros_info.discovery_server_disabled = True
                self.ros_info.automatic_discovery_range = self._fallback_discovery_range
                self._refresh_hello_payload()

        applied = apply_ros_network_env(self.ros_info)
        if self.ros_info.discovery_server:
            # HostNode performs graph introspection and must receive all endpoint
            # metadata from Discovery Server v2, not only pre-matched topics.
            os.environ["ROS_SUPER_CLIENT"] = "TRUE"
            applied["ROS_SUPER_CLIENT"] = "TRUE"
        else:
            os.environ.pop("ROS_SUPER_CLIENT", None)
        if applied:
            logger.info("[EdgeMicrobackend] applied Host ROS config: %s", applied)

        try:
            self.server.start()
        except Exception:
            if self._discovery_server is not None:
                self._discovery_server.stop()
                self._discovery_server = None
            raise
        set_hostlink_server(self.server)
        logger.info(
            "[EdgeMicrobackend] HostLink owns slave networking on %s:%s",
            HostLinkConfig.bind,
            self.server.port,
        )
        return self

    def stop(self) -> None:
        self.server.stop()
        if self._discovery_server is not None:
            self._discovery_server.stop()
            self._discovery_server = None
        set_hostlink_server(None)

    def attach_resource_tree(self, getter: ResourceTreeGetter) -> None:
        """Attach HostNode runtime state without transferring network ownership."""

        with self._resource_lock:
            self._resource_tree_getter = getter

    def _resource_tree(self) -> Any:
        with self._resource_lock:
            getter = self._resource_tree_getter
        return getter() if getter is not None else None

    def _material(self, data: dict[str, Any], _peer: dict[str, Any]) -> dict[str, Any]:
        """Serve the host material component, then its attached runtime tree."""

        from unilabos.app.web.client import http_client

        resource_uuid = data.get("uuid") or None
        resource_id = data.get("id") or None
        with_children = bool(data.get("with_children", True))
        try:
            nodes = http_client.material_query(
                uuids=[resource_uuid] if resource_uuid else None,
                resource_id=resource_id,
                with_children=with_children,
            )
        except Exception as exc:  # noqa: BLE001 - runtime tree is compatibility fallback
            logger.warning(
                "[EdgeMicrobackend] material HTTP source failed; "
                "falling back to HostNode runtime tree: %s",
                exc,
            )
            nodes = []
        if not nodes:
            nodes = self._resolver.resolve(
                uuid=resource_uuid,
                res_id=resource_id,
                with_children=with_children,
            )
        return {"nodes": nodes}

    def _ros_info(
        self,
        _data: dict[str, Any],
        _peer: dict[str, Any],
    ) -> dict[str, Any]:
        return {"owner": SERVICE_OWNER, "ros": self.ros_info.to_dict()}


_host_service_lock = threading.Lock()
_host_service: Optional[HostNetworkService] = None
_slave_setup_lock = threading.Lock()
_cleanup_registration_lock = threading.Lock()
_cleanup_registered = False


def _register_process_cleanup() -> None:
    """Register one idempotent finalizer for normal interpreter shutdown."""

    global _cleanup_registered
    with _cleanup_registration_lock:
        if _cleanup_registered:
            return
        atexit.register(shutdown_network_services)
        _cleanup_registered = True


def setup_host_network_service(
    resource_tree_getter: Optional[ResourceTreeGetter] = None,
) -> Optional[HostNetworkService]:
    """Start the host listener once and optionally attach live HostNode state."""

    global _host_service
    if not HostLinkConfig.enable:
        return None
    with _host_service_lock:
        if _host_service is not None:
            if resource_tree_getter is not None:
                _host_service.attach_resource_tree(resource_tree_getter)
            return _host_service
        try:
            _host_service = HostNetworkService.from_config(resource_tree_getter).start()
            _register_process_cleanup()
        except Exception as exc:  # noqa: BLE001 - ROS fallback remains available
            logger.error(
                "[EdgeMicrobackend] HostLink start failed (ROS-only fallback): %s",
                exc,
            )
            _host_service = None
        return _host_service


def get_host_network_service() -> Optional[HostNetworkService]:
    with _host_service_lock:
        return _host_service


def shutdown_host_network_service() -> None:
    global _host_service
    with _host_service_lock:
        service, _host_service = _host_service, None
    if service is not None:
        service.stop()


def startup_device_ids(devices_config: Any) -> list[str]:
    """Return all ``type=device`` IDs reported by this Slave's startup graph."""

    result: list[str] = []
    for node in getattr(devices_config, "all_nodes", []) or []:
        content = getattr(node, "res_content", None)
        if getattr(content, "type", "") != "device":
            continue
        device_id = str(getattr(content, "id", "") or "").strip()
        if device_id and device_id not in result:
            result.append(device_id)
    return result


def require_slave_startup_device_ids(devices_config: Any) -> list[str]:
    """Reject a runtime Slave graph that cannot provide a business identity."""

    device_ids = startup_device_ids(devices_config)
    if not device_ids:
        raise ValueError(
            "Slave 启动图必须至少包含一个 type=device 节点；测试请使用 virtual/mock "
            "设备，空设备图仅允许 Host 启动。"
        )
    return device_ids


def setup_slave_network_client(
    *,
    device_ids: Optional[Iterable[str]] = None,
    wait_for_host: Optional[bool] = None,
) -> Tuple[Optional[HostLinkClient], Optional[int]]:
    """Start the Slave link and, by default, wait for Host before ROS init.

    Normal Slave mode must receive the Host ROS policy before ``rclpy.init``.
    ``--slave_no_host`` is the explicit offline/degraded mode: it starts the
    reconnect manager but never blocks ROS startup waiting for the first Host.
    ``wait_for_host`` exists for direct embedders and tests; omitted means derive
    the behavior from :class:`BasicConfig`.
    """

    if not HostLinkConfig.enable or not HostLinkConfig.host:
        return None, None
    if wait_for_host is None:
        wait_for_host = not BasicConfig.slave_no_host
    with _slave_setup_lock:
        client = get_hostlink_client()
        if client is None:
            client = HostLinkClient(
                HostLinkConfig.host,
                HostLinkConfig.port,
                machine_name=BasicConfig.machine_name,
                device_ids=device_ids,
                heartbeat_interval=HostLinkConfig.heartbeat_interval,
                connect_timeout=HostLinkConfig.connect_timeout,
                request_timeout=HostLinkConfig.request_timeout,
            )
            set_hostlink_client(client)
            _register_process_cleanup()
        elif device_ids:
            client.configure_device_ids(device_ids)

    # Do not hold the singleton lock during an unbounded required-Host wait;
    # shutdown and another idempotent setup call must remain able to reach the
    # same client.
    if client.online:
        connected = True
    elif wait_for_host:
        logger.info(
            "[EdgeMicrobackend] waiting for required Host %s:%s before ROS init; "
            "use --slave_no_host only for intentional offline startup",
            HostLinkConfig.host,
            HostLinkConfig.port,
        )
        connected = client.connect_blocking(timeout=None)
    else:
        client.start()
        connected = client.online

    if not connected:
        if wait_for_host:
            logger.warning(
                "[EdgeMicrobackend] required Host wait stopped before %s:%s became online",
                HostLinkConfig.host,
                HostLinkConfig.port,
            )
        else:
            logger.warning(
                "[EdgeMicrobackend] --slave_no_host: Host %s:%s is not online; "
                "starting with local ROS config while HostLink keeps retrying",
                HostLinkConfig.host,
                HostLinkConfig.port,
            )
        return client, None

    ros_info = client.hello_ros_info()
    if not HostLinkConfig.ros_assist_apply:
        logger.info(
            "[EdgeMicrobackend] ros_assist_apply=False: keep HostLink, skip ROS config"
        )
        return client, None

    if HostLinkConfig.host not in ros_info.static_peers:
        ros_info.static_peers.append(HostLinkConfig.host)
    if ros_info.discovery_server and ros_info.discovery_server_managed:
        ros_info.discovery_server = use_connected_host(
            ros_info.discovery_server,
            HostLinkConfig.host,
        )
    applied = apply_ros_network_env(ros_info)
    if applied:
        logger.info("[EdgeMicrobackend] applied host ROS config: %s", applied)
    return client, ros_info.domain_id


def shutdown_slave_network_client() -> None:
    with _slave_setup_lock:
        client = get_hostlink_client()
        set_hostlink_client(None)
    if client is not None:
        client.close()


def shutdown_network_services() -> None:
    shutdown_slave_network_client()
    shutdown_host_network_service()


__all__ = [
    "HostNetworkService",
    "SERVICE_OWNER",
    "get_host_network_service",
    "require_slave_startup_device_ids",
    "setup_host_network_service",
    "setup_slave_network_client",
    "startup_device_ids",
    "shutdown_host_network_service",
    "shutdown_network_services",
    "shutdown_slave_network_client",
]
