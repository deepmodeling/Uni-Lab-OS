"""验证软件包注册表代际（Package Registry Generation）的深模块边界。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.registry.package_generation import PackageRegistryGeneration


class _Snapshot:
    """提供可控候选集合和唯一短名解析的软件包注册表快照替身。"""

    def __init__(
        self,
        *,
        devices: dict[str, Any],
        resources: dict[str, Any],
        aliases: dict[tuple[str, str], str] | None = None,
        failure: Exception | None = None,
    ) -> None:
        """保存测试所需的候选、短名和可选失败。

        参数：``devices`` 与 ``resources`` 是待发布候选；``aliases`` 把种类和
        短名映射到规范全限定身份（FQID）；``failure`` 要求候选构造抛出的异常。
        返回：无。
        异常：无；失败行为延迟到 ``registry_candidates`` 调用。
        """

        self._devices = devices
        self._resources = resources
        self._aliases = aliases or {}
        self._failure = failure

    def to_dict(self) -> dict[str, Any]:
        """返回包含主包与外部包静态定义的完整查询投影。

        参数：无。
        返回：每次调用均新建、可由查询调用方修改而不影响当前权威代际的字典。
        异常：无。
        """

        return {
            "packages": ["workspace-lab", "external-lab"],
            "devices": sorted(self._devices),
            "resources": sorted(self._resources),
            "workflows": ["community.external_lab.inspect_external"],
            "assets": ["community.external_lab:models/shape.yml"],
        }

    def registry_candidates(
        self,
        _original_devices: dict[str, Any],
        _original_resources: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回完整候选集合或模拟候选构造失败。

        参数：``_original_devices`` 与 ``_original_resources`` 是旧代际映射，本
        替身无需读取。
        返回：设备与资源候选字典的独立浅副本。
        异常：配置 ``failure`` 时原样抛出，以验证失败不产生部分发布。
        """

        if self._failure is not None:
            raise self._failure
        return dict(self._devices), dict(self._resources)

    def resolve(self, kind: str, identity: str) -> SimpleNamespace:
        """把唯一兼容短名解析为规范定义身份。

        参数：``kind`` 是设备或资源种类；``identity`` 是待解析短名。
        返回：仅含 ``fqid`` 的定义替身。
        异常：短名未配置时抛出 ``KeyError``，模拟关闭式身份解析。
        """

        # ``canonical_identity`` 是当前快照确认的规范全限定身份（FQID）。
        canonical_identity = self._aliases[(kind, identity)]
        return SimpleNamespace(fqid=canonical_identity)


def test_generation_publish_is_atomic_and_preserves_previous_snapshot() -> None:
    """候选失败不得修改设备、资源或当前软件包注册表快照。

    参数：无。
    返回：无；断言成功代际整体可见，后续失败仍解析到成功代际。
    异常：若出现部分发布或快照先行替换，断言失败。
    """

    # ``registry`` 是只承载两类实时权威映射的最小注册表替身。
    registry = SimpleNamespace(
        device_type_registry={"builtin.device": {"version": "builtin"}},
        resource_type_registry={"builtin.resource": {"version": "builtin"}},
    )
    generation = PackageRegistryGeneration(registry)
    published_device = {"version": "published"}
    published_resource = {"version": "published"}
    successful_snapshot = _Snapshot(
        devices={"community.lab.device": published_device},
        resources={"community.lab.resource": published_resource},
        aliases={
            ("device", "device"): "community.lab.device",
            ("resource", "resource"): "community.lab.resource",
        },
    )

    generation.publish(successful_snapshot)

    assert registry.device_type_registry == {
        "community.lab.device": published_device,
    }
    assert registry.resource_type_registry == {
        "community.lab.resource": published_resource,
    }
    assert generation.resolve("device", "device") is published_device
    assert generation.resolve("resource", "resource") is published_resource

    failed_snapshot = _Snapshot(
        devices={"community.next.device": {"version": "partial"}},
        resources={"community.next.resource": {"version": "partial"}},
        failure=RuntimeError("候选校验失败"),
    )
    with pytest.raises(RuntimeError, match="候选校验失败"):
        generation.publish(failed_snapshot)

    assert registry.device_type_registry == {
        "community.lab.device": published_device,
    }
    assert registry.resource_type_registry == {
        "community.lab.resource": published_resource,
    }
    assert generation.resolve("device", "device") is published_device


def test_generation_resolves_exact_identity_before_package_alias() -> None:
    """实时精确身份应优先于软件包兼容短名解析。

    参数：无。
    返回：无；断言内置精确身份不依赖已发布软件包快照。
    异常：若解析器错误要求快照或跨种类查询，断言失败。
    """

    exact_device = {"version": "builtin"}
    registry = SimpleNamespace(
        device_type_registry={"builtin.device": exact_device},
        resource_type_registry={},
    )
    generation = PackageRegistryGeneration(registry)

    assert generation.resolve("device", "builtin.device") is exact_device
    with pytest.raises(KeyError, match="定义不存在"):
        generation.resolve("resource", "missing")
    with pytest.raises(ValueError, match="种类"):
        generation.resolve("workflow", "builtin.device")  # type: ignore[arg-type]


def test_generation_exposes_detached_complete_snapshot_projection() -> None:
    """注册表（Registry）必须稳定查询完整且与调用方修改隔离的包目录代。

    参数：无。
    返回：无；断言查询投影同时包含主包、外部包、设备、资源、显式工作流
    （Workflow）与资产，且修改一次返回值不会改变后续查询。
    异常：未发布时查询或快照不提供查询投影应关闭式失败；若泄漏内部可变容器，
    第二次查询断言失败。
    """

    registry = SimpleNamespace(
        device_type_registry={},
        resource_type_registry={},
    )
    generation = PackageRegistryGeneration(registry)
    with pytest.raises(RuntimeError, match="尚未发布"):
        generation.snapshot_projection()

    snapshot = _Snapshot(
        devices={"community.external_lab.reader": {"version": "1"}},
        resources={"community.external_lab.plate": {"version": "1"}},
    )
    generation.publish(snapshot)
    first_projection = generation.snapshot_projection()
    first_projection["packages"].append("caller-mutation")
    second_projection = generation.snapshot_projection()

    assert second_projection == {
        "packages": ["workspace-lab", "external-lab"],
        "devices": ["community.external_lab.reader"],
        "resources": ["community.external_lab.plate"],
        "workflows": ["community.external_lab.inspect_external"],
        "assets": ["community.external_lab:models/shape.yml"],
    }


def test_product_registry_delegates_complete_snapshot_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """产品注册表（Registry）公开同一代完整包目录查询而不建立第二投影权威。

    参数：``monkeypatch`` 隔离进程级产品注册表映射和包目录发布代。
    返回：无；断言 ``package_snapshot`` 直接查询当前
    ``PackageRegistryGeneration``，并保留外部显式工作流（Workflow）与资产。
    异常：产品注册表缺少查询委派或自行重建不完整目录时断言失败。
    """

    from unilabos.registry.registry import lab_registry

    monkeypatch.setattr(lab_registry, "device_type_registry", {})
    monkeypatch.setattr(lab_registry, "resource_type_registry", {})
    # ``product_package_generation`` 是本例唯一的包目录（PackageCatalog）发布与
    # 查询权威。
    product_package_generation = PackageRegistryGeneration(lab_registry)
    monkeypatch.setattr(
        lab_registry,
        "_package_generation",
        product_package_generation,
    )
    snapshot = _Snapshot(
        devices={"community.external_lab.reader": {"version": "1"}},
        resources={"community.external_lab.plate": {"version": "1"}},
    )

    lab_registry.publish_package_snapshot(snapshot)

    assert lab_registry.package_snapshot()["workflows"] == [
        "community.external_lab.inspect_external"
    ]
    assert lab_registry.package_snapshot()["assets"] == [
        "community.external_lab:models/shape.yml"
    ]
