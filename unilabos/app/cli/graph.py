"""``unilab graph`` 设备图管理命令。

默认操作本机微后端 materials 域的 graphs 端点；``--remote`` 切换到会话/
配置中的云端地址（协议同构）。``create`` 在本地扫描 ``@device`` 生成图
骨架，不需要任何后端在线。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from unilabos.client import (
    EnvelopeError,
    SessionManager,
    print_error,
    print_output,
    print_success,
    print_warning,
)
from unilabos.client.materials.graph import HTTPGraphClient
from unilabos.config.config import BasicConfig


def _create_graph_client(
    args: Any,
    session_manager: SessionManager,
) -> HTTPGraphClient:
    """默认本机微后端；``--remote`` 时强制使用云端 base_url。"""

    from unilabos.app.cli.auth_resolver import resolve_effective_auth

    with session_manager:
        effective = resolve_effective_auth(args, session_manager)

    if getattr(args, "remote", False):
        base_url = effective["base_url"]
        if not base_url:
            print_error(
                "--remote 需要云端地址。请先 unilab login --addr <云端地址>，"
                "或本次命令附加 --address <云端地址>"
            )
            raise SystemExit(1)
    else:
        port = getattr(args, "port_management", None) or BasicConfig.port
        base_url = f"http://127.0.0.1:{port}"

    return HTTPGraphClient(
        base_url,
        ak=effective["ak"],
        sk=effective["sk"],
    )


def _registry_resource_lookup(client: HTTPGraphClient) -> Callable[[str], Optional[dict[str, Any]]]:
    """旧格式图上传用的「模板名 → 注册表资源条目」：查上传目标调度权威的注册表（Host 上报的目录）。

    只在图里真有缺 PLR 类的旧物料节点时才发请求；先确认权威收到过注册表上报（否则所有模板都
    查不到，报「未上报」比报「模板不在注册表」更准确），再按模板逐个查并缓存。
    """

    from unilabos.server.backend.legacy_adaptor.legacy.graph import LegacyGraphError

    cache: dict[str, Optional[dict[str, Any]]] = {}
    checked = False

    def lookup(name: str) -> Optional[dict[str, Any]]:
        nonlocal checked
        if name in cache:
            return cache[name]
        if not checked:
            try:
                status = client.registry_status()
            except Exception as error:  # noqa: BLE001 - 连不上 / 路由不存在 / 信封错误都归为「读不到注册表」
                raise LegacyGraphError(
                    f"旧格式图需要按模板查注册表补齐 PLR 类名，但无法读取 {client.base_url} 的注册表: {error}"
                ) from error
            if not (isinstance(status, dict) and status.get("reported")):
                raise LegacyGraphError(
                    f"{client.base_url} 的调度权威尚未收到 Host 的注册表上报，无法按模板补齐旧格式图的"
                    " PLR 类名；先启动 Host 再上传"
                )
            checked = True
        try:
            detail = client.registry_entry(name)
        except EnvelopeError as error:
            # 条目不存在是业务码 4001（HTTP 语义 404）；其余错误照实报
            if error.status_code != 404:
                raise LegacyGraphError(f"查询注册表模板 {name!r} 失败: {error}") from error
            detail = None
        payload = detail.get("active_payload") if isinstance(detail, dict) else None
        status_flags = detail.get("status") if isinstance(detail, dict) else None
        usable = (
            isinstance(payload, dict)
            and payload.get("registry_type") == "resource"
            and "removed" not in (status_flags or [])
        )
        cache[name] = payload if usable else None
        return cache[name]

    return lookup


def _load_graph_file(file_path: str, client: HTTPGraphClient) -> dict[str, Any]:
    """读取待上传的图文件；旧格式图在此转成当前契约，Graph Authority 只收当前契约。"""

    path = Path(file_path)
    if not path.is_file():
        print_error(f"graph 文件不存在: {file_path}")
        raise SystemExit(1)
    try:
        with path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        print_error(f"读取 graph JSON 失败: {error}")
        raise SystemExit(1) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        print_error("graph payload 必须是含 nodes 数组的 JSON 对象")
        raise SystemExit(1)

    from unilabos.server.backend.legacy_adaptor.legacy.graph import upgrade_legacy_graph_payload

    return upgrade_legacy_graph_payload(
        payload,
        source=f"graph 文件 {file_path}",
        report=print_warning,
        resource_entry=_registry_resource_lookup(client),
    )


def _cmd_create(args: Any) -> None:
    """扫描 @device 生成 node-link 图骨架并写入文件。"""

    devices_dirs = args.devices or []
    if not devices_dirs:
        print_error("graph create 需要 --devices 指定设备包目录")
        raise SystemExit(1)

    from unilabos.registry.registry import build_registry

    registry = build_registry(
        registry_paths=None,
        devices_dirs=devices_dirs,
        upload_registry=False,
        external_only=True,
    )
    from unilabos.registry.graph_scaffold import build_graph_skeleton

    payload = build_graph_skeleton(registry, include=args.include or None)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print_success(
        f"graph 骨架已生成: {output}（{len(payload['nodes'])} 个设备节点）"
    )


def cmd_graph_command(args: Any, session_manager: SessionManager) -> None:
    """分发 graph 子命令；create 纯本地，其余走 Graph Authority API。"""

    action = str(args.graph_command)
    if action == "create":
        _cmd_create(args)
        return

    client = None
    try:
        client = _create_graph_client(args, session_manager)
        if action == "upload":
            payload = _load_graph_file(args.graph_file, client)
            name = args.graph_name or Path(args.graph_file).stem
            data = client.upsert_graph(
                name=name,
                payload=payload,
                tags=args.tags or [],
                description=args.description or None,
            )
            summary = data.get("summary") or {}
            counts = {
                key: len(summary.get(key) or [])
                for key in ("created", "updated", "removed", "unchanged")
            }
            assigned = int(summary.get("uuid_assigned") or 0)
            detail = (
                f"节点 新建 {counts['created']} / 更新 {counts['updated']} / "
                f"移除 {counts['removed']} / 不变 {counts['unchanged']}"
            )
            if assigned:
                detail += f"，发号 {assigned} 个身份"
            print_output(
                data,
                message=(
                    f"graph 已上传: {data.get('name')} "
                    f"(uuid={data.get('uuid')}, revision={data.get('revision')})，{detail}"
                ),
            )
        elif action == "list":
            print_output(
                client.list_graphs(
                    page=args.page,
                    page_size=args.page_size,
                    name=args.name or "",
                )
            )
        elif action == "get":
            print_output(client.get_graph(args.identity))
        elif action == "download":
            payload = client.download_graph(args.identity)
            output = Path(args.output or f"{args.identity}.json")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print_success(f"graph 已下载: {output}")
        elif action == "delete":
            client.delete_graph(args.identity)
            print_success(f"graph 已删除: {args.identity}")
        else:
            raise ValueError(f"不支持的 graph 子命令: {action}")
    except SystemExit:
        raise
    except (EnvelopeError, OSError, ValueError) as error:
        code = getattr(error, "code", None)
        prefix = f"[{code}] " if code is not None else ""
        print_error(f"graph 命令失败: {prefix}{error}")
        raise SystemExit(1) from error
    finally:
        if client is not None:
            client.close()


__all__ = ["cmd_graph_command"]
