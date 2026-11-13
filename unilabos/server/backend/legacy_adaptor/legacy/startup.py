"""旧云端 Backend 相关的启动期接线。

- :func:`upgrade_startup_graph_payload` —— ``-g`` 启动图读取边界：旧后端导出图 /
  dev 时代示例图在这里整图转成当前 node-link 契约，之后的 Graph Authority（登记与
  运行时装配）只见当前契约。这是 ``unilabos.app.main`` 唯一触碰旧后端的地方；
- :func:`start_legacy_uplink` —— 显式接入旧协议 Backend 时的开机上联：注册表
  上报、物料全量镜像与增量镜像线程。Edge 启动不再调用它；由 Backend 侧的
  legacy 兼容入口与 :meth:`BackendSessionFactory.create_legacy_client` 一起装配。

剥离旧后端支持时删除本模块与 ``main`` 里的启动图调用点即可。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

from unilabos.config.config import HTTPConfig
from unilabos.server.backend.legacy_adaptor.legacy.graph import upgrade_legacy_graph_payload
from unilabos.server.backend.legacy_adaptor.legacy.http import LegacyBackendHTTPClient
from unilabos.server.backend.legacy_adaptor.legacy.sync import (
    LegacyMaterialMirror,
    upload_registry_snapshot,
)
from unilabos.utils.banner_print import print_status


def upgrade_startup_graph_payload(payload: Mapping[str, Any], file_path: str) -> Dict[str, Any]:
    """``-g`` 文件读入后的唯一旧格式入口：识别到旧字段就整图转换并打 WARNING。"""

    return upgrade_legacy_graph_payload(
        payload,
        source=f"启动图 {file_path}",
        report=lambda message: print_status(message, "warning"),
    )


def start_legacy_uplink(
    lab_registry: Any,
    *,
    materials_gateway: Any,
    resource_links: Sequence[Mapping[str, Any]],
) -> LegacyMaterialMirror:
    """旧云端 Backend 的开机上联；返回已启动的物料镜像，进程退出时 ``stop()``。

    注册表走 ``/lab/resource``，物料镜像走 ``/edge/material``；两者都是
    fail-open：失败只打日志，不阻断启动。
    """

    print_status(
        f"显式接入旧协议 Backend: {HTTPConfig.remote_addr}，启用 legacy 适配",
        "warning",
    )
    client = LegacyBackendHTTPClient()
    report = upload_registry_snapshot(lab_registry, client)
    if report is not None:
        print_status(
            f"注册表已上报旧 Backend: 设备 {report.device_count}"
            f"{'（未变化）' if report.device_skipped else ''} "
            f"资源 {report.resource_count}"
            f"{'（未变化）' if report.resource_skipped else ''}",
            "info",
        )
    mirror = LegacyMaterialMirror(
        client=client,
        gateway=materials_gateway,
        known_templates=set(report.template_ids if report is not None else ()),
    )
    try:
        mirror.upload_full(links=resource_links)
    except Exception as exc:  # noqa: BLE001 - 镜像失败不阻断启动
        print_status(f"物料全量镜像到旧 Backend 失败（不影响运行）: {exc}", "warning")
    mirror.start()
    return mirror


__all__ = ["start_legacy_uplink", "upgrade_startup_graph_payload"]
