"""``--root_devices_as_slaves``：Host 启动时把设备图的每个根设备连同子树放进各自的受管 Slave。

Host 本身只做组网与调度对接，每个根设备由自己的受管 Slave 子进程
（:mod:`unilabos.server.services.device_processes`）启动和运行：一台设备构造失败、卡住或需要重启，
都只动它自己的进程，Host 与其它设备照常服务。

拆分规则（:func:`split_root_device_subtrees`）：

- 根设备 = 图中没有父节点的 ``type=device`` 节点；``host_node`` 留在 Host；
- 根设备的整棵子树（子设备、物料、Site）进同一个 Slave；节点 dict 原样取自 Graph Authority 的
  payload（uuid、config、parent 都不变），两端都在子树里的连线一并带上；
- 根级非设备物料、跨子树的连线留在 Host 的图里；
- 设备类不支持当前 backend 的根设备留在 Host：Host 照旧把它登记为不可用并给出原因。

完整的图照旧登记在 Graph Authority，Host 照旧对齐全部开机物料，设备身份不变。

规格生命周期（:func:`prepare_root_device_slaves`）：每个根设备一条 ``generated_by=root_devices_as_slaves``
的规格，id 由设备 id 确定（:func:`generated_spec_id`），重启 Host 复用同一条；图里没有了的设备的规格随之
移除；不带本参数启动的 Host 移除全部生成规格。手建规格从不改动；与手建规格 id 相同的设备留在 Host。
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from unilabos.utils.banner_print import print_status

if TYPE_CHECKING:
    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.server.services.device_processes import DeviceProcessSpec

#: 生成规格的来源标记（``DeviceProcessSpec.generated_by``），也是 Host 启动参数名
GENERATED_BY = "root_devices_as_slaves"
SPEC_ID_PREFIX = "root-"
_PLAIN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
#: Slave 只认自己那份配置的 HostLink 客户端参数；Host 命令行给了就原样传下去
_INHERITED_HOSTLINK_ARGS = (
    "hostlink_heartbeat_interval",
    "hostlink_connect_timeout",
    "hostlink_host_wait_timeout",
    "hostlink_request_timeout",
)
#: 影响设备运行 / 注册表的开关：Host 带了，Slave 也带
_INHERITED_SWITCHES = (("test_mode", "--test_mode"), ("extra_resource", "--extra_resource"), ("2d_vis", "--2d_vis"))


@dataclass(frozen=True)
class RootDeviceSubtree:
    """一个根设备及其整棵子树（node-link 节点 dict 与内部连线，均为副本）。"""

    device_id: str
    uuid: str
    template_name: str
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def device_ids(self) -> List[str]:
        return [str(node["id"]) for node in self.nodes if str(node.get("type") or "") == "device"]


def generated_spec_id(device_id: str) -> str:
    """根设备对应的受管进程 id：``root-<设备 id>``；含路径 / URL 不安全字符或过长的 id 改为
    ``root-<净化后的 id>-<由原 id 派生的 12 位十六进制>``，仍只由设备 id 决定。"""

    device_id = str(device_id).strip()
    if _PLAIN_ID.match(device_id):
        return f"{SPEC_ID_PREFIX}{device_id}"
    slug = _UNSAFE_ID_CHARS.sub("_", device_id).strip("_")[:40] or "device"
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, f"unilab:root-device-slave:{device_id}").hex[:12]
    return f"{SPEC_ID_PREFIX}{slug}-{suffix}"


def _link_endpoint_inside(link: Mapping[str, Any], side: str, ids: set[str], uuids: set[str]) -> bool:
    endpoint_uuid = str(link.get(f"{side}_uuid") or "").strip()
    if endpoint_uuid:
        return endpoint_uuid in uuids
    return str(link.get(side) or "").strip() in ids


def split_root_device_subtrees(
    payload: Mapping[str, Any],
    tree_set: "ResourceTreeSet",
    *,
    host_node_name: str = "",
    keep_in_host: Optional[Callable[[Mapping[str, Any]], bool]] = None,
) -> List[RootDeviceSubtree]:
    """按 ``tree_set``（由同一 payload 装配的运行时树）的层级切出要进 Slave 的根设备子树。

    节点按 uuid 从 ``payload`` 取原件（顺序与 payload 一致），所以子树与 Host 自己会启动的完全一致；
    ``keep_in_host(根节点 dict)`` 为真的根设备不切出。
    """

    from unilabos.client.host.runtime.definition import is_host_node_config

    nodes = [node for node in payload.get("nodes") or [] if isinstance(node, Mapping)]
    links = [link for link in payload.get("links", payload.get("edges")) or [] if isinstance(link, Mapping)]
    by_uuid = {str(node.get("uuid") or ""): node for node in nodes if node.get("uuid")}
    subtrees: List[RootDeviceSubtree] = []
    for tree in tree_set.trees:
        root = tree.root_node.res_content
        if str(root.type or "") != "device" or is_host_node_config(root, host_node_name):
            continue
        root_node = by_uuid.get(str(root.uuid or ""))
        if root_node is None:
            # 运行时树来自同一 payload；对不上说明 payload 被改过，宁可留在 Host 也不猜
            print_status(f"根设备 {root.id} 在 Graph Authority payload 里找不到同 uuid 节点，留在 Host 运行", "warning")
            continue
        if keep_in_host is not None and keep_in_host(root_node):
            continue
        member_uuids = {str(node.res_content.uuid) for node in tree.get_all_nodes()}
        members = [copy.deepcopy(dict(node)) for node in nodes if str(node.get("uuid") or "") in member_uuids]
        member_ids = {str(node.get("id") or "") for node in members}
        inner_links = [
            copy.deepcopy(dict(link))
            for link in links
            if _link_endpoint_inside(link, "source", member_ids, member_uuids)
            and _link_endpoint_inside(link, "target", member_ids, member_uuids)
        ]
        subtrees.append(
            RootDeviceSubtree(
                device_id=str(root.id),
                uuid=str(root.uuid),
                template_name=str(root.template_name or ""),
                nodes=members,
                links=inner_links,
            )
        )
    return subtrees


def root_devices_as_slaves_enabled(args_dict: Dict[str, Any]) -> bool:
    """参数只对运行设备的 Host 生效：``--is_slave`` / ``--role backend`` 下告警并忽略。"""

    if not args_dict.get("root_devices_as_slaves"):
        return False
    if args_dict.get("is_slave"):
        reason = "Slave 没有受管设备进程"
    elif args_dict.get("role") == "backend":
        reason = "--role backend 只运行调度权威，不运行设备"
    else:
        return True
    print_status(f"--root_devices_as_slaves 只对 Host 生效，已忽略：{reason}", "warning")
    args_dict["root_devices_as_slaves"] = False
    return False


def graph_is_authority_payload_enabled(args_dict: Dict[str, Any]) -> bool:
    """``--graph_is_authority_payload`` 只给 Slave 用：Host 的启动图必须登记进自己的 Graph Authority。"""

    if not args_dict.get("graph_is_authority_payload"):
        return False
    if not args_dict.get("is_slave"):
        print_status(
            "--graph_is_authority_payload 只对 Slave 生效（Host 的启动图必须登记进 Graph Authority），已忽略",
            "warning",
        )
        args_dict["graph_is_authority_payload"] = False
        return False
    return True


def check_authority_payload_file(file_path: str) -> Optional[str]:
    """``-g`` 文件能否按 Graph Authority payload 原样启动；不能时返回原因。"""

    from unilabos.server.services.materials.graph import GraphError, validate_graph_payload

    try:
        with open(file_path, encoding="utf-8") as stream:
            payload = json.load(stream)
        validate_graph_payload(payload)
    except (OSError, json.JSONDecodeError, GraphError) as exc:
        return str(exc)
    missing = [
        str(node.get("id") or "?") if isinstance(node, dict) else "?"
        for node in payload["nodes"]
        if not isinstance(node, dict) or not str(node.get("uuid") or "").strip()
    ]
    if missing:
        return f"节点没有 uuid（不是 Graph Authority 的 payload）：{', '.join(missing[:5])}"
    return None


def _inherited_args(args_dict: Dict[str, Any], *, working_dir: str, config_path: Optional[str]) -> List[str]:
    """Slave 要与 Host 用同一套工作目录、配置与运行开关；图按 Host 权威 payload 原样启动。"""

    extra = ["--graph_is_authority_payload", "--working_dir", os.path.abspath(working_dir)]
    if config_path and os.path.isfile(config_path):
        extra += ["--config", os.path.abspath(config_path)]
    for path in args_dict.get("registry_path") or []:
        extra += ["--registry_path", os.path.abspath(str(path))]
    for key, flag in _INHERITED_SWITCHES:
        if args_dict.get(key):
            extra.append(flag)
    for key in _INHERITED_HOSTLINK_ARGS:
        value = args_dict.get(key)
        if value is not None:
            extra += [f"--{key}", str(value)]
    return extra


def build_root_device_specs(
    subtrees: List[RootDeviceSubtree],
    args_dict: Dict[str, Any],
    *,
    working_dir: str,
    config_path: Optional[str],
) -> List["DeviceProcessSpec"]:
    """每个根设备子树一条规格：Host 的 ``--devices`` / ``--external_devices_only``、默认重启策略、自动启动。"""

    from unilabos.server.services.device_processes import DeviceProcessSpec

    devices_dirs = list(dict.fromkeys(os.path.abspath(str(item)) for item in args_dict.get("devices") or []))
    extra_args = _inherited_args(args_dict, working_dir=working_dir, config_path=config_path)
    return [
        DeviceProcessSpec(
            id=generated_spec_id(subtree.device_id),
            name=subtree.device_id,
            graph_nodes=subtree.nodes,
            graph_links=subtree.links,
            devices_dirs=list(devices_dirs),
            external_only=bool(args_dict.get("external_devices_only")),
            auto_start=True,
            extra_args=list(extra_args),
            generated_by=GENERATED_BY,
        )
        for subtree in subtrees
    ]


def _unsupported_on_backend(backend: str) -> Callable[[Mapping[str, Any]], bool]:
    """``keep_in_host``：设备类明确不支持当前 backend 时为真（Host 照旧登记不可用并给出原因）；
    模板未知按支持处理，让它在自己的 Slave 里失败。"""

    def keep_in_host(node: Mapping[str, Any]) -> bool:
        from unilabos.client.host import BackendConfigurationError, resolve_driver_backends
        from unilabos.registry.registry import lab_registry

        entry = lab_registry.device_type_registry.get(str(node.get("template_name") or ""))
        class_config = entry.get("class") if isinstance(entry, dict) else None
        if not isinstance(class_config, dict):
            return False
        try:
            return backend not in resolve_driver_backends(class_config)
        except BackendConfigurationError:
            return False

    return keep_in_host


def _read_authority_payload(file_path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not file_path or not os.path.isfile(file_path):
        return None
    with open(file_path, encoding="utf-8") as stream:
        payload = json.load(stream)
    return payload if isinstance(payload, dict) else None


def prepare_root_device_slaves(
    args_dict: Dict[str, Any],
    resource_tree_set: "ResourceTreeSet",
    *,
    working_dir: str,
    config_path: Optional[str],
) -> "ResourceTreeSet":
    """Host 启动时同步生成规格，返回 Host 自己的设备运行时要启动的树集。

    参数关闭（或 HostLink 被关掉）时移除上次生成的规格、原样返回 ``resource_tree_set``；开启时为每个根
    设备生成规格，返回去掉这些子树后的树集。规格在 Host 就绪后由 ``start_auto`` 拉起。
    """

    from unilabos.config.config import BasicConfig, HostLinkConfig
    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.server.services.device_processes import (
        PROCESS_DIRNAME,
        load_specs,
        peek_device_process_service,
        sync_generated_specs,
    )

    enabled = bool(args_dict.get("root_devices_as_slaves"))
    if enabled and not HostLinkConfig.enable:
        print_status(
            "--root_devices_as_slaves 需要 HostLink（受管 Slave 经它接回 Host）；HostLink 已关闭，设备留在 Host 运行",
            "warning",
        )
        enabled = False
    if not enabled and not any(spec.generated_by == GENERATED_BY for spec in load_specs(working_dir).values()):
        return resource_tree_set

    subtrees: List[RootDeviceSubtree] = []
    if enabled:
        payload = _read_authority_payload(args_dict.get("_graph_file_path"))
        if payload is not None:
            subtrees = split_root_device_subtrees(
                payload,
                resource_tree_set,
                host_node_name=str(BasicConfig.host_node_name or ""),
                keep_in_host=_unsupported_on_backend(str(BasicConfig.backend or "hostlink")),
            )
    specs = build_root_device_specs(subtrees, args_dict, working_dir=working_dir, config_path=config_path)
    # 启动早期服务单例通常还不存在：只改文件，不提前创建它（它的 atexit 收尾顺序保持不变）
    service = peek_device_process_service()
    if service is not None:
        summary = service.sync_generated(GENERATED_BY, specs)
    else:
        summary = sync_generated_specs(working_dir, GENERATED_BY, specs)
    if not enabled:
        print_status(
            f"未带 --root_devices_as_slaves：已移除上次生成的受管设备进程 {', '.join(summary['removed'])}，设备在 Host 运行",
            "info",
        )
        return resource_tree_set

    conflicts = set(summary["conflicts"])
    for spec_id in sorted(conflicts):
        print_status(f"受管设备进程 id {spec_id} 已被手建规格占用，对应根设备留在 Host 运行", "warning")
    moved = [subtree for subtree in subtrees if generated_spec_id(subtree.device_id) not in conflicts]
    moved_uuids = {subtree.uuid for subtree in moved}
    kept = [tree for tree in resource_tree_set.trees if str(tree.root_node.res_content.uuid) not in moved_uuids]
    if not moved:
        print_status("--root_devices_as_slaves：设备图里没有可拆出的根设备，设备照常在 Host 运行", "info")
        return resource_tree_set
    names = ", ".join(f"{subtree.device_id}（{len(subtree.device_ids)} 台设备）" for subtree in moved)
    print_status(f"--root_devices_as_slaves：{len(moved)} 个根设备各自运行在受管 Slave 里：{names}", "info")
    print_status(
        f"各根设备的 Slave 日志：{os.path.join(os.path.abspath(working_dir), PROCESS_DIRNAME)}"
        f"{os.sep}<进程 id>{os.sep}process.log（进程 id 为 {SPEC_ID_PREFIX}<设备 id>，见 /api/v1/device-processes）",
        "info",
    )
    return ResourceTreeSet(kept)


__all__ = [
    "GENERATED_BY",
    "RootDeviceSubtree",
    "build_root_device_specs",
    "check_authority_payload_file",
    "generated_spec_id",
    "graph_is_authority_payload_enabled",
    "prepare_root_device_slaves",
    "root_devices_as_slaves_enabled",
    "split_root_device_subtrees",
]
