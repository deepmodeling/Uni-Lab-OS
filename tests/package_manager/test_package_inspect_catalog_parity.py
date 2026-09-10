"""``package inspect`` 与包目录（PackageCatalog）同源合同。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.package_manager.test_package_catalog_compiler import _write_package


def test_package_inspect_uses_the_same_catalog_compiler(
    tmp_path: Path,
) -> None:
    """证明 ``package inspect`` 输出与公开静态编译器完全一致。

    参数：``tmp_path`` 提供隔离软件包和输出目录。
    返回：无；断言设备定义、目录摘要和规范目录文件来自同一编译结果。
    异常：旧扫描路径产生不同定义或摘要时断言失败。
    """

    from unilabos.package_manager import (
        WorkspaceSource,
        compile_package_source,
        inspect_package,
    )

    # ``workspace_root`` 是命令行与注册表（Registry）共同读取的软件包来源。
    workspace_root = tmp_path / "workspace"
    output_root = tmp_path / "output"
    _write_package(workspace_root)
    # ``expected_catalog`` 是公共静态编译缝给出的行为参照。
    expected_catalog = compile_package_source(WorkspaceSource(workspace_root))

    # ``inspection`` 是公共 package 子命令产生的兼容输出汇总。
    inspection = inspect_package(
        str(workspace_root),
        namespace=None,
        out_dir=str(output_root),
    )
    # ``catalog_document`` 是命令落盘、可供工具读取的规范目录文档。
    catalog_document = json.loads(
        Path(inspection["catalog_path"]).read_text(encoding="utf-8")
    )

    assert sorted(inspection["devices"]) == ["reactor"]
    assert inspection["catalog_digest"] == expected_catalog.catalog_digest
    assert catalog_document == expected_catalog.to_dict()


def test_package_inspect_reports_device_count_separately_from_resource_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人类摘要不得把设备与物料模板投影总数误称为设备数。

    参数：``tmp_path`` 提供同时含设备和资源定义的软件包；``monkeypatch`` 捕获
    状态输出 Adapter。
    返回：无；断言设备数来自设备身份集合，资源投影数使用独立中文标签。
    异常：检查编译或产物写入失败时传播原始异常；统计标签回归时断言失败。
    """

    from unilabos.package_manager import inspect_package
    from unilabos.package_manager.package_distribution import inspection

    # ``workspace_root`` 是包含一台设备和一个物料模板定义的规范工作区。
    workspace_root = tmp_path / "workspace"
    _write_package(workspace_root)
    # ``messages`` 保存检查摘要实际发送给终端 Adapter 的中文行。
    messages: list[str] = []

    def record_status(message: str, _level: str) -> None:
        """记录一条终端状态输出而不打印。

        参数：``message`` 是摘要文本；``_level`` 是本测试不解释的显示级别。
        返回：无；消息按调用顺序追加。
        异常：无。
        """

        messages.append(message)

    monkeypatch.setattr(inspection, "print_status", record_status)

    inspect_package(str(workspace_root), out_dir=str(tmp_path / "dist"))

    assert "  设备数          : 1 (reactor)" in messages
    assert "  资源投影数      : 2" in messages
