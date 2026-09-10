"""把 Edge Registry 作为一个完整模板图同步到正式后端。"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

import requests

from unilabos.registry.template_snapshot import (
    RegistryTemplateSnapshot,
    RegistryTemplateSnapshotError,
)
from unilabos.utils.tracing import inject_trace_context, span


DEVELOPER_TOKEN_ENV = "UNILAB_TEMPLATE_SYNC_DEVELOPER_TOKEN"


class TemplateSyncError(RuntimeError):
    """模板收集或后端事务同步失败。"""


@dataclass(frozen=True)
class TemplateSyncReport:
    """一次完整同步的稳定结果，供初始化 Job 和测试流程消费。"""

    device_count: int
    resource_count: int
    template_uuids: Dict[str, str]


class TemplateSynchronizer:
    """隐藏 Registry 遍历、协议映射、压缩和 HTTP 提交细节。"""

    def __init__(
        self,
        backend_address: str,
        developer_token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 60.0,
    ) -> None:
        """创建模板同步器并固定一次同步所需的 Backend 连接参数。

        参数说明：``backend_address`` 是 Backend 服务地址；``developer_token``
        是初始化任务使用的开发者凭据；``session`` 是可替换的 HTTP 适配器；
        ``timeout`` 是单次同步请求的秒级超时。无返回值；凭据缺失时抛出
        ``TemplateSyncError``，避免发送无权写入的模板事务。
        """

        token = str(developer_token or "").strip()
        if not token:
            raise TemplateSyncError("developer token is required")
        self.backend_api = _api_base(backend_address)
        self.developer_token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def sync(self, registry: Any) -> TemplateSyncReport:
        """通过一个不可变 Registry 快照执行一次 Backend 事务同步。

        参数说明：``registry`` 可以是已编译的 ``RegistryTemplateSnapshot``，也可
        以兼容方式传入 Registry；后者只遍历一次并立即冻结。返回同步报告。
        """

        try:
            # ``registry_snapshot`` 是本次同步唯一的不可变设备注册表定义代际；
            # 后续设备模板和资源模板都从它分离复制，禁止再次遍历活 Registry。
            registry_snapshot = (
                registry
                if isinstance(registry, RegistryTemplateSnapshot)
                else RegistryTemplateSnapshot.from_registry(registry)
            )
        except RegistryTemplateSnapshotError as error:
            raise TemplateSyncError(str(error)) from error
        # ``devices`` 与 ``resources`` 是发送给 Backend 的相同快照副本，
        # ``definitions`` 保留完整事务成员集合，供身份回执做全量核对。
        devices = registry_snapshot.detached_devices()
        resources = registry_snapshot.detached_resources()
        definitions = [*devices, *resources]
        if not definitions:
            raise TemplateSyncError("Edge Registry does not contain any templates")

        payload = {"resources": definitions}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.developer_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        url = f"{self.backend_api}/resource-templates"
        target = urlsplit(url)
        with span(
            "edge.http.template.sync",
            kind="client",
            attributes={
                "http.request.method": "POST",
                "http.route": "/api/v1/resource-templates",
                "server.address": target.hostname or "",
                "template.device.count": len(devices),
                "template.resource.count": len(resources),
            },
        ) as request_span:
            inject_trace_context(headers)
            response = self.session.post(
                url,
                data=gzip.compress(encoded),
                headers=headers,
                timeout=self.timeout,
            )
            try:
                request_span.set_attribute(
                    "http.response.status_code", response.status_code
                )
            except Exception:  # noqa: BLE001 - tracing must remain fail-open
                pass

        result = _decode_sync_response(response)
        template_uuids = {
            str(identity["name"]): str(identity["uuid"])
            for identity in result.get("templates", [])
            if isinstance(identity, Mapping)
            and identity.get("name")
            and identity.get("uuid")
        }
        expected_names = {definition["id"] for definition in definitions}
        if set(template_uuids) != expected_names:
            missing = sorted(expected_names - set(template_uuids))
            raise TemplateSyncError(
                f"backend response is missing template identities: {missing}"
            )
        return TemplateSyncReport(
            device_count=len(devices),
            resource_count=len(resources),
            template_uuids=template_uuids,
        )


def sync_registry_from_environment(
    registry: Any,
    backend_address: str,
    *,
    session: Optional[requests.Session] = None,
) -> TemplateSyncReport:
    """使用初始化 Job 注入的开发者身份同步一个 Registry。"""

    developer_token = os.environ.get(DEVELOPER_TOKEN_ENV, "")
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(registry)


def run_template_sync_command(
    arguments: Mapping[str, Any],
    *,
    backend_address: str,
    environment: Optional[Mapping[str, str]] = None,
    registry_builder: Optional[Callable[..., Any]] = None,
    session: Optional[requests.Session] = None,
) -> TemplateSyncReport:
    """执行独立初始化命令，不进入设备图和驱动启动流程。"""

    if registry_builder is None:
        from unilabos.registry.registry import build_registry

        registry_builder = build_registry
    registry = registry_builder(
        registry_paths=arguments.get("registry_path"),
        devices_dirs=arguments.get("devices"),
        # 这里必须保持纯静态收集；旧开关会实例化 PyLabRobot 器材来补配置，
        # 会把初始化 Job 意外耦合到驱动和硬件环境。
        upload_registry=False,
        complete_registry=bool(arguments.get("complete_registry", False)),
        external_only=bool(arguments.get("external_devices_only", False)),
    )
    token_source = environment if environment is not None else os.environ
    developer_token = token_source.get(DEVELOPER_TOKEN_ENV, "")
    return TemplateSynchronizer(
        backend_address,
        developer_token,
        session=session,
    ).sync(registry)


def _decode_sync_response(response: Any) -> Dict[str, Any]:
    """校验 Backend 模板事务响应并返回模板稳定身份映射载荷。

    参数说明：``response`` 是 HTTP 适配器返回的响应对象。返回 ``data`` 中的
    JSON 对象；HTTP、业务错误或模板身份结构不完整时抛出
    ``TemplateSyncError``，禁止调用者接受部分同步结果。
    """

    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise TemplateSyncError(
            f"template sync returned non-JSON HTTP {response.status_code}"
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise TemplateSyncError(
            f"template sync returned HTTP {response.status_code}: {payload}"
        )
    if not isinstance(payload, Mapping):
        raise TemplateSyncError("template sync returned a non-object response")
    code = int(payload.get("code") or 0)
    if code != 0:
        raise TemplateSyncError(
            f"template sync returned business error {code}: {payload.get('error')}"
        )
    result = payload.get("data", payload)
    if not isinstance(result, Mapping) or not isinstance(
        result.get("templates"), list
    ):
        raise TemplateSyncError("template sync returned invalid template identities")
    return dict(result)


def _api_base(address: str) -> str:
    """把 Backend 地址规范化为唯一的 ``/api/v1`` 接口前缀。

    参数说明：``address`` 是用户或启动配置提供的 Backend 地址。返回去除末尾
    斜杠后的接口根地址；空地址抛出 ``TemplateSyncError``。
    """

    base = str(address or "").strip().rstrip("/")
    if not base:
        raise TemplateSyncError("backend address is required")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"
