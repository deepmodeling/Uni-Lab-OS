"""软件包依赖锁（Package Dependency Lock）的公开行为合同。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from unilabos.package_manager.workspace_runtime import compile_package_source


def _write_package(
    root: Path,
    *,
    distribution_name: str,
    package_name: str,
    device_ids: tuple[str, ...] = (),
    resource_ids: tuple[str, ...] = (),
) -> None:
    """写入一个可由统一静态编译器读取的最小软件包工作区。

    参数：``root`` 是软件包根；``distribution_name`` 是发行身份；
    ``package_name`` 是导入包身份；``device_ids`` 与 ``resource_ids`` 是待登记
    的完整设备和资源定义集合。
    返回：无；工作区文件直接写入隔离测试目录。
    异常：测试文件系统写入失败时传播原始异常。
    """

    package_root = root / package_name
    package_root.mkdir(parents=True, exist_ok=True)
    root.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "{distribution_name}"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    root.joinpath("package.yaml").write_text(
        f"package: {{name: {package_name}}}\nworkflows: []\n",
        encoding="utf-8",
    )
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    definitions = [
        "from unilabos.registry.decorators import device, resource",
        "",
    ]
    for device_id in device_ids:
        symbol = "".join(part.title() for part in device_id.split("_"))
        definitions.extend(
            (
                f'@device(id="{device_id}", category=["test"])',
                f"class {symbol}:",
                "    pass",
                "",
            )
        )
    for resource_id in resource_ids:
        definitions.extend(
            (
                f'@resource(id="{resource_id}", category=["test"])',
                f"def make_{resource_id}(name: str):",
                "    return name",
                "",
            )
        )
    package_root.joinpath("definitions.py").write_text(
        "\n".join(definitions),
        encoding="utf-8",
    )


def test_add_publishes_explicit_declaration_and_verified_lock(
    tmp_path: Path,
) -> None:
    """新增外部包须先完整编译，再同时发布显式声明和可重放锁。

    参数：``tmp_path`` 提供主工作区和外部软件包的隔离父目录。
    返回：无；断言锁定目录可重新加载全部设备和资源定义，且不会产生具体
    物料（Material）。
    异常：若实现依赖 ambient site-packages、遗漏定义或只写一个文件则断言失败。
    """

    from unilabos.package_manager import (
        PackageDependencyManager,
        load_locked_package_catalogs,
    )

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external_lab"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader", "incubator"),
        resource_ids=("plate",),
    )

    # ``dependency_lock`` 是显式软件包来源成功校验后发布的当前依赖代际。
    dependency_lock = PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    ).add("../external_lab")
    catalogs = load_locked_package_catalogs(
        workspace_root,
        compile_catalog=compile_package_source,
    )

    assert workspace_root.joinpath("unilabos.packages.yaml").is_file()
    assert workspace_root.joinpath("unilabos.packages.lock.json").is_file()
    assert tuple(item.distribution_name for item in dependency_lock.packages) == (
        "external-lab",
    )
    assert len(catalogs) == 1
    assert tuple(item.id for item in catalogs[0].definitions.devices) == (
        "incubator",
        "reader",
    )
    assert tuple(item.id for item in catalogs[0].definitions.resources) == ("plate",)
    assert not hasattr(catalogs[0], "materials")


def test_update_relocks_changed_source_without_changing_dependency_identity(
    tmp_path: Path,
) -> None:
    """更新只允许同一发行身份推进内容摘要并重新完整校验聚合目录。

    参数：``tmp_path`` 提供主工作区和可修改外部包。
    返回：无；断言新设备定义进入完整锁定目录且旧依赖身份保持稳定。
    异常：若更新叠加第二项依赖或没有核对新目录，断言失败。
    """

    from unilabos.package_manager import (
        PackageDependencyManager,
        load_locked_package_catalogs,
    )

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external_lab"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
    )
    manager = PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    )
    first_lock = manager.add("../external_lab")
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader", "incubator"),
    )

    # ``second_lock`` 是同一显式依赖在源码更新后的新锁定代际。
    second_lock = manager.update("external-lab")
    catalogs = load_locked_package_catalogs(
        workspace_root,
        compile_catalog=compile_package_source,
    )

    assert len(second_lock.packages) == 1
    assert second_lock.packages[0].normalized_name == "external_lab"
    assert second_lock.packages[0].catalog_digest != (
        first_lock.packages[0].catalog_digest
    )
    assert tuple(item.id for item in catalogs[0].definitions.devices) == (
        "incubator",
        "reader",
    )


def test_remove_revalidates_remaining_dependencies_and_publishes_empty_lock(
    tmp_path: Path,
) -> None:
    """删除依赖后保留一对明确空声明和空锁，不回退环境扫描。

    参数：``tmp_path`` 提供主工作区与待删除外部包。
    返回：无；断言删除后加载结果为空且两个权威文件仍成对存在。
    异常：依赖不存在或剩余聚合定义无效时应由实现关闭式失败。
    """

    from unilabos.package_manager import (
        PackageDependencyManager,
        load_locked_package_catalogs,
    )

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external_lab"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        resource_ids=("plate",),
    )
    manager = PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    )
    manager.add("../external_lab")

    dependency_lock = manager.remove("community.external_lab")

    assert dependency_lock.packages == ()
    assert (
        load_locked_package_catalogs(
            workspace_root,
            compile_catalog=compile_package_source,
        )
        == ()
    )
    assert workspace_root.joinpath("unilabos.packages.yaml").is_file()
    assert workspace_root.joinpath("unilabos.packages.lock.json").is_file()


def test_ambient_site_packages_are_never_discovered_without_explicit_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可导入的 ambient site-packages 不能成为工作区外部包来源。

    参数：``tmp_path`` 提供空依赖主工作区和伪环境目录；``monkeypatch`` 临时修改
    ``sys.path``。
    返回：无；断言没有显式声明和锁时目录集合保持为空。
    异常：实现若扫描导入环境会错误发现伪包并使断言失败。
    """

    from unilabos.package_manager import load_locked_package_catalogs

    workspace_root = tmp_path / "workspace"
    ambient_root = tmp_path / "ambient"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        ambient_root,
        distribution_name="ambient-lab",
        package_name="ambient_lab",
        device_ids=("hidden",),
    )
    monkeypatch.setattr(sys, "path", [str(ambient_root), *sys.path])

    assert (
        load_locked_package_catalogs(
            workspace_root,
            compile_catalog=compile_package_source,
        )
        == ()
    )


def test_failed_conflicting_add_keeps_declaration_and_lock_byte_identical(
    tmp_path: Path,
) -> None:
    """候选跨包身份冲突必须在任何依赖文件写入前关闭式失败。

    参数：``tmp_path`` 提供主工作区、既有依赖和冲突候选包。
    返回：无；断言失败前后的声明和锁字节完全一致。
    异常：冲突应传播关闭式错误；若出现部分写入，字节断言失败。
    """

    from unilabos.package_manager import (
        PackageDependencyError,
        PackageDependencyManager,
    )

    workspace_root = tmp_path / "workspace"
    first_root = tmp_path / "first"
    duplicate_root = tmp_path / "duplicate"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        first_root,
        distribution_name="shared-lab",
        package_name="shared_lab",
        device_ids=("reader",),
    )
    _write_package(
        duplicate_root,
        distribution_name="shared.lab",
        package_name="shared_lab",
        resource_ids=("plate",),
    )
    manager = PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    )
    manager.add("../first")
    declaration_path = workspace_root / "unilabos.packages.yaml"
    lock_path = workspace_root / "unilabos.packages.lock.json"
    before = (declaration_path.read_bytes(), lock_path.read_bytes())

    with pytest.raises(PackageDependencyError, match="已存在|重复|冲突"):
        manager.add("../duplicate")

    assert (declaration_path.read_bytes(), lock_path.read_bytes()) == before
    assert json.loads(lock_path.read_text(encoding="utf-8"))["schema_version"] == "1"


def test_source_drift_fails_closed_until_explicit_update(
    tmp_path: Path,
) -> None:
    """锁定来源发生变化后，普通加载不能把新内容当作已授权代际。

    参数：``tmp_path`` 提供主工作区与会发生源码漂移的外部包。
    返回：无；断言重编译摘要不一致时提示显式 update。
    异常：若加载器自动接受磁盘新内容或回退环境包，断言失败。
    """

    from unilabos.package_manager import (
        PackageDependencyError,
        PackageDependencyManager,
        load_locked_package_catalogs,
    )

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
    )
    PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    ).add("../external")
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader", "incubator"),
    )

    with pytest.raises(PackageDependencyError, match="update"):
        load_locked_package_catalogs(
            workspace_root,
            compile_catalog=compile_package_source,
        )


def test_update_rejects_replacement_with_different_distribution_before_write(
    tmp_path: Path,
) -> None:
    """更新来源不得把既有依赖身份静默替换为另一个发行包。

    参数：``tmp_path`` 提供主工作区、既有依赖和不同身份替代包。
    返回：无；断言身份变化失败且声明与锁字节保持不变。
    异常：实现若把 update 当 remove+add 或先写后验，断言失败。
    """

    from unilabos.package_manager import (
        PackageDependencyError,
        PackageDependencyManager,
    )

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    replacement_root = tmp_path / "replacement"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
    )
    _write_package(
        replacement_root,
        distribution_name="other-lab",
        package_name="other_lab",
        device_ids=("reader",),
    )
    manager = PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_package_source,
    )
    manager.add("../external")
    declaration_path = workspace_root / "unilabos.packages.yaml"
    lock_path = workspace_root / "unilabos.packages.lock.json"
    before = (declaration_path.read_bytes(), lock_path.read_bytes())

    with pytest.raises(PackageDependencyError, match="不得改变发行身份"):
        manager.update("external-lab", "../replacement")

    assert (declaration_path.read_bytes(), lock_path.read_bytes()) == before
