"""注册表快照（Registry Snapshot）的有限激活与原子发布合同。"""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.workspace_runtime.activation import (
    publish_registry_snapshot,
)


class _RejectingRegistryMap(dict[str, Any]):
    """模拟在发布阶段拒绝写入的注册表映射。"""

    def __setitem__(self, key: str, value: Any) -> None:
        """拒绝单项注册表写入以制造可观察的发布失败。

        参数：``key`` 是待发布定义身份；``value`` 是待发布注册表条目。
        返回：无；本测试替身始终拒绝修改。
        异常：始终抛出 ``RuntimeError``，用于验证失败后没有部分发布。
        """

        raise RuntimeError("模拟资源注册表发布失败")

    def update(self, *args: Any, **kwargs: Any) -> None:
        """拒绝批量注册表写入以覆盖原地更新实现。

        参数：``args`` 和 ``kwargs`` 是 ``dict.update`` 兼容输入，仅用于识别调用。
        返回：无；本测试替身始终拒绝修改。
        异常：始终抛出 ``RuntimeError``，用于验证原子发布回滚。
        """

        raise RuntimeError("模拟资源注册表批量发布失败")


class _FailingRegistry:
    """同时拦截原地更新和属性替换的失败注册表测试替身。"""

    def __init__(self) -> None:
        """建立包含既有内置定义的注册表初始事实。

        参数：无。
        返回：无；设备和资源映射都保留可比较的内置定义。
        异常：无。
        """

        self._device_type_registry: dict[str, Any] = {
            "host_node": {"source": "builtin"}
        }
        self._resource_type_registry = _RejectingRegistryMap(
            {"builtin_plate": {"source": "builtin"}}
        )

    @property
    def device_type_registry(self) -> dict[str, Any]:
        """返回当前设备注册表事实。

        参数：无。
        返回：可由被测发布流程读取或原地修改的设备定义映射。
        异常：无。
        """

        return self._device_type_registry

    @device_type_registry.setter
    def device_type_registry(self, value: Mapping[str, Any]) -> None:
        """接受设备注册表整体替换，以便观察后续资源发布失败的回滚。

        参数：``value`` 是被测流程准备发布的完整设备注册表映射。
        返回：无；保存与调用者容器分离的副本。
        异常：无。
        """

        self._device_type_registry = dict(value)

    @property
    def resource_type_registry(self) -> _RejectingRegistryMap:
        """返回拒绝写入的资源注册表事实。

        参数：无。
        返回：可读取但任何原地发布都会失败的映射。
        异常：无。
        """

        return self._resource_type_registry

    @resource_type_registry.setter
    def resource_type_registry(self, value: Mapping[str, Any]) -> None:
        """拒绝资源注册表整体替换以制造第二阶段发布失败。

        参数：``value`` 是待发布的完整资源注册表映射，仅用于触发失败路径。
        返回：无；资源注册表保持原值。
        异常：始终抛出 ``RuntimeError``，验证设备注册表不会残留部分修改。
        """

        raise RuntimeError("模拟资源注册表整体发布失败")


def _write_registry_workspace(
    root: Path,
    *,
    distribution: str,
    import_package: str,
    device_ids: tuple[str, ...] = (),
    resource_ids: tuple[str, ...] = (),
) -> WorkspaceSource:
    """写入一个包含指定静态注册表定义的测试工作区。

    参数：``root`` 是授权工作区根；``distribution`` 是发行包身份；
    ``import_package`` 是唯一 Python 导入包；``device_ids`` 是设备定义短身份；
    ``resource_ids`` 是资源定义短身份。
    返回：只授权该根目录的软件包来源 Adapter。
    异常：文件系统写入失败时传播原始异常；生成内容不导入作者模块。
    """

    # ``package_root`` 是包目录（PackageCatalog）允许扫描的唯一导入包边界。
    package_root = root / import_package
    package_root.mkdir(parents=True)
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    root.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "{distribution}"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    root.joinpath("package.yaml").write_text(
        f"package: {{name: {import_package}}}\nworkflows: []\n",
        encoding="utf-8",
    )
    definition_lines = [
        "import builtins",
        "from unilabos.registry.decorators import device, resource",
        "",
        "builtins._registry_snapshot_author_source_imported = True",
        "",
    ]
    for definition_id in device_ids:
        # ``symbol`` 是作者模块内的 Python 符号，不承担跨包规范身份。
        symbol = "Device" + definition_id.title().replace("_", "")
        definition_lines.extend(
            (
                f'@device(id="{definition_id}", category=["test"])',
                f"class {symbol}:",
                "    pass",
                "",
            )
        )
    for definition_id in resource_ids:
        # ``symbol`` 是资源工厂源码符号；注册表身份仍由规范全限定身份承担。
        symbol = "make_" + definition_id
        definition_lines.extend(
            (
                f'@resource(id="{definition_id}", category=["test"])',
                f"def {symbol}(name: str):",
                "    return name",
                "",
            )
        )
    package_root.joinpath("definitions.py").write_text(
        "\n".join(definition_lines),
        encoding="utf-8",
    )
    return WorkspaceSource(root)


def _device_graph(*definition_identities: str) -> dict[str, Any]:
    """构造只引用指定设备身份的最小物理图输入。

    参数：``definition_identities`` 是图节点声明的规范全限定身份或兼容短身份。
    返回：每个身份对应一个设备节点的图数据新字典。
    异常：无；身份合法性由注册表快照（Registry Snapshot）关闭式校验。
    """

    return {
        "nodes": [
            {
                "id": f"runtime-device-{index}",
                "class": identity,
                "type": "device",
            }
            for index, identity in enumerate(definition_identities)
        ]
    }


def _compile_snapshot(*sources: WorkspaceSource):
    """经确认的公开缝编译一个注册表快照（Registry Snapshot）。

    参数：``sources`` 是本次启动显式授权的全部软件包来源。
    返回：由完整包目录（PackageCatalog）集合编译的不可变注册表快照。
    异常：公共接口缺失或任一目录冲突时传播被测实现异常。
    """

    from unilabos.package_manager import compile_registry_snapshot

    # ``catalogs`` 是启动观察到的完整包目录（PackageCatalog）集合。
    catalogs = tuple(compile_package_source(source) for source in sources)
    return compile_registry_snapshot(catalogs)


def test_complete_package_is_queryable_while_graph_selects_one_device(
    tmp_path: Path,
) -> None:
    """证明完整包定义可查询，而物理图只产生有限设备激活计划。

    参数：``tmp_path`` 提供含两个设备的隔离工作区。
    返回：无；断言快照保留完整目录，但激活计划只包含图引用的设备。
    异常：若未发现完整包或错误激活未引用设备，断言失败。
    """

    # ``source`` 是同时声明反应器和加热器的单一外部软件包来源。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="finite-activation-lab",
        import_package="finite_activation_lab",
        device_ids=("reactor", "heater"),
    )
    snapshot = _compile_snapshot(source)

    assert tuple(item.fqid for item in snapshot.devices) == (
        "community.finite_activation_lab.heater",
        "community.finite_activation_lab.reactor",
    )
    assert (
        snapshot.resolve("device", "community.finite_activation_lab.reactor").id
        == "reactor"
    )

    # ``activation_plan`` 是物理图有限选择出的运行激活责任，不是完整目录替身。
    activation_plan = snapshot.select(
        _device_graph("community.finite_activation_lab.heater")
    )

    assert tuple(item.fqid for item in activation_plan.devices) == (
        "community.finite_activation_lab.heater",
    )
    assert activation_plan.resources == ()


def test_snapshot_compilation_and_selection_never_import_author_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明快照编译与有限选择都不会执行作者模块顶层代码。

    参数：``tmp_path`` 提供隔离工作区；``monkeypatch`` 清理进程导入哨兵。
    返回：无；断言完成编译、解析和选择后哨兵仍不存在。
    异常：任何作者源码导入都会设置哨兵并使测试失败。
    """

    # ``source`` 是作者模块携带可观察顶层副作用的受控软件包来源。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="no-import-lab",
        import_package="no_import_lab",
        device_ids=("reader",),
    )
    monkeypatch.delattr(
        builtins,
        "_registry_snapshot_author_source_imported",
        raising=False,
    )

    snapshot = _compile_snapshot(source)
    snapshot.resolve("device", "reader")
    snapshot.select(_device_graph("reader"))

    assert not hasattr(builtins, "_registry_snapshot_author_source_imported")


def test_canonical_fqid_and_unique_short_identity_resolve_to_same_definition(
    tmp_path: Path,
) -> None:
    """证明规范全限定身份与唯一兼容短名解析为同一静态定义。

    参数：``tmp_path`` 提供只含一个对应短名的隔离工作区。
    返回：无；断言两种身份不会复制或改写定义身份。
    异常：短名不唯一或全限定身份不存在时被测实现应关闭式失败。
    """

    # ``source`` 是让 ``pump`` 在全部已授权包中保持唯一的来源。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="identity-lab",
        import_package="identity_lab",
        device_ids=("pump",),
    )
    snapshot = _compile_snapshot(source)

    canonical = snapshot.resolve("device", "community.identity_lab.pump")
    legacy_alias = snapshot.resolve("device", "pump")

    assert canonical is legacy_alias
    assert canonical.fqid == "community.identity_lab.pump"


def test_cross_package_short_identity_ambiguity_fails_closed(
    tmp_path: Path,
) -> None:
    """证明跨包重复短名不能被物理图隐式选择。

    参数：``tmp_path`` 提供两个均声明 ``shared`` 的隔离软件包。
    返回：无；断言有限激活拒绝歧义短名，但规范身份仍可查询。
    异常：被测接口应抛出含“歧义”诊断的 ``ValueError``。
    """

    # 两个 ``source`` 共同制造只影响兼容短名、不会破坏规范身份的歧义。
    first_source = _write_registry_workspace(
        tmp_path / "first",
        distribution="ambiguous-first",
        import_package="ambiguous_first",
        device_ids=("shared",),
    )
    second_source = _write_registry_workspace(
        tmp_path / "second",
        distribution="ambiguous-second",
        import_package="ambiguous_second",
        device_ids=("shared",),
    )
    snapshot = _compile_snapshot(first_source, second_source)

    assert (
        snapshot.resolve("device", "community.ambiguous_first.shared").fqid
        == "community.ambiguous_first.shared"
    )
    with pytest.raises(ValueError, match="歧义"):
        snapshot.select(_device_graph("shared"))


def test_missing_community_fqid_fails_closed_without_short_name_fallback(
    tmp_path: Path,
) -> None:
    """证明缺失社区全限定身份不会回退到同短名的其他包定义。

    参数：``tmp_path`` 提供含同短名设备但不同命名空间的工作区。
    返回：无；断言物理图引用缺失命名空间时直接拒绝激活。
    异常：被测接口应抛出含“不存在”诊断的 ``ValueError``。
    """

    # ``source`` 只提供 available 命名空间，不能满足 missing 命名空间引用。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="available-lab",
        import_package="available_lab",
        device_ids=("reader",),
    )
    snapshot = _compile_snapshot(source)

    with pytest.raises(ValueError, match="不存在"):
        snapshot.select(_device_graph("community.missing_lab.reader"))


def test_publish_preserves_builtins_and_registers_the_complete_package(
    tmp_path: Path,
) -> None:
    """证明发布保留内置定义并登记完整包，而不是只登记激活子集。

    参数：``tmp_path`` 提供含两个设备和一个资源的隔离软件包。
    返回：无；断言发布后内置条目与全部包定义同时存在。
    异常：重复身份或无效注册表条目应由被测实现关闭式拒绝。
    """

    # ``source`` 同时提供被图选择和未选择定义，验证发布集合始终完整。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="complete-publish-lab",
        import_package="complete_publish_lab",
        device_ids=("reactor", "heater"),
        resource_ids=("plate",),
    )
    snapshot = _compile_snapshot(source)
    registry = SimpleNamespace(
        device_type_registry={"host_node": {"source": "builtin"}},
        resource_type_registry={"builtin_plate": {"source": "builtin"}},
    )

    publish_registry_snapshot(snapshot, registry)

    assert set(registry.device_type_registry) == {
        "host_node",
        "community.complete_publish_lab.heater",
        "community.complete_publish_lab.reactor",
    }
    assert set(registry.resource_type_registry) == {
        "builtin_plate",
        "community.complete_publish_lab.plate",
    }
    assert registry.device_type_registry["host_node"] == {"source": "builtin"}


def test_publish_failure_leaves_both_registry_collections_unchanged(
    tmp_path: Path,
) -> None:
    """证明任一注册表集合发布失败时不会留下部分设备定义。

    参数：``tmp_path`` 提供同时含设备和资源定义的隔离软件包。
    返回：无；断言失败后两个集合仍精确等于发布前事实。
    异常：测试替身制造 ``RuntimeError``，被测发布接口必须向调用者传播。
    """

    # ``source`` 确保设备阶段可成功、资源阶段可制造中途失败。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="atomic-publish-lab",
        import_package="atomic_publish_lab",
        device_ids=("reactor",),
        resource_ids=("plate",),
    )
    snapshot = _compile_snapshot(source)
    registry = _FailingRegistry()
    original_devices = dict(registry.device_type_registry)
    original_resources = dict(registry.resource_type_registry)

    with pytest.raises(RuntimeError, match="模拟资源注册表"):
        publish_registry_snapshot(snapshot, registry)

    assert registry.device_type_registry == original_devices
    assert registry.resource_type_registry == original_resources


def test_catalog_order_does_not_change_snapshot_fingerprint_or_immutability(
    tmp_path: Path,
) -> None:
    """证明目录输入顺序不影响快照指纹，且发布前快照不可被改写。

    参数：``tmp_path`` 提供两个内容固定但传入顺序相反的软件包来源。
    返回：无；断言规范指纹相等、定义顺序稳定且冻结字段拒绝赋值。
    异常：修改冻结快照应抛出 ``FrozenInstanceError``。
    """

    from unilabos.package_manager import compile_registry_snapshot

    # 两个 ``catalog`` 是相同启动集合，只有调用者提供的目录顺序不同。
    first_catalog = compile_package_source(
        _write_registry_workspace(
            tmp_path / "first",
            distribution="order-first",
            import_package="order_first",
            device_ids=("first",),
        )
    )
    second_catalog = compile_package_source(
        _write_registry_workspace(
            tmp_path / "second",
            distribution="order-second",
            import_package="order_second",
            device_ids=("second",),
        )
    )

    forward = compile_registry_snapshot((first_catalog, second_catalog))
    reverse = compile_registry_snapshot((second_catalog, first_catalog))

    assert forward.fingerprint == reverse.fingerprint
    assert forward.fingerprint.startswith("sha256:")
    assert forward.devices == reverse.devices
    assert isinstance(forward.devices, tuple)
    with pytest.raises(FrozenInstanceError):
        forward.fingerprint = "sha256:" + "0" * 64
