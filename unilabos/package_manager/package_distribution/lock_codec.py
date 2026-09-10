"""显式软件包依赖声明与锁文件的关闭式编解码。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from .models import (
    DEPENDENCY_DECLARATION_FILE,
    DEPENDENCY_LOCK_FILE,
    PackageDependencyError,
    PackageDependencyLock,
)


def load_dependency_state(
    workspace_root: Path,
) -> tuple[dict[str, tuple[str, str]], PackageDependencyLock]:
    """读取必须成对出现的显式依赖声明和锁。

    参数：``workspace_root`` 是主工作区规范根。
    返回：以规范发行身份索引的声明和不可变锁；两文件均不存在时返回空代际。
    异常：只存在一个文件、YAML/JSON 形状无效或声明身份重复时抛出
    ``PackageDependencyError``。
    """

    # 两个路径共同保存同一依赖代际，任何一方缺失都不得被解释为空依赖。
    declaration_path = workspace_root / DEPENDENCY_DECLARATION_FILE
    lock_path = workspace_root / DEPENDENCY_LOCK_FILE
    if not declaration_path.exists() and not lock_path.exists():
        return {}, PackageDependencyLock()
    if not declaration_path.is_file() or not lock_path.is_file():
        raise PackageDependencyError("软件包依赖声明和锁必须成对存在")
    if declaration_path.is_symlink() or lock_path.is_symlink():
        raise PackageDependencyError("软件包依赖声明和锁不得是符号链接")
    try:
        # ``document`` 是依赖声明 YAML 解码后的未验证根对象。
        document = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise PackageDependencyError("软件包依赖声明不是合法 YAML") from error
    if not isinstance(document, dict) or document.get("schema_version") != "1":
        raise PackageDependencyError("软件包依赖声明版本无效")
    # ``raw_dependencies`` 是声明文件中尚未验证的依赖数组。
    raw_dependencies = document.get("dependencies")
    if not isinstance(raw_dependencies, list) or any(
        not isinstance(item, dict) for item in raw_dependencies
    ):
        raise PackageDependencyError("软件包依赖声明 dependencies 必须是对象数组")
    # ``declarations`` 以规范发行身份索引原始发行名和可移植来源。
    declarations: dict[str, tuple[str, str]] = {}
    for item in raw_dependencies:
        # 三个字段共同证明声明身份和来源可以与规范锁一一核对。
        distribution_name = item.get("name")
        source = item.get("source")
        normalized_name = item.get("normalized_name")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (distribution_name, normalized_name, source)
        ):
            raise PackageDependencyError("软件包依赖声明字段无效")
        if normalized_name in declarations:
            raise PackageDependencyError("软件包依赖声明身份重复")
        declarations[normalized_name] = (distribution_name, source)
    try:
        # ``dependency_lock`` 是规范 JSON 解码和模型校验后的依赖代际。
        dependency_lock = PackageDependencyLock.from_bytes(lock_path.read_bytes())
    except OSError as error:
        raise PackageDependencyError("软件包依赖锁不可读取") from error
    # ``locked_declarations`` 只投影锁中应与 YAML 声明完全一致的字段。
    locked_declarations = {
        item.normalized_name: (item.distribution_name, item.source)
        for item in dependency_lock.packages
    }
    if locked_declarations != declarations:
        raise PackageDependencyError("软件包依赖声明与锁不一致")
    return declarations, dependency_lock


def declaration_bytes(
    declarations: Mapping[str, tuple[str, str]],
) -> bytes:
    """生成稳定、独立于原 YAML 格式的显式依赖声明。

    参数：``declarations`` 以规范发行身份索引名称和来源。
    返回：字段稳定排序且末尾带换行的 UTF-8 YAML。
    异常：无；调用前字段已验证。
    """

    # ``payload`` 是写入声明文件的唯一稳定结构，不保留输入 YAML 格式差异。
    payload = {
        "schema_version": "1",
        "dependencies": [
            {
                "name": declarations[key][0],
                "normalized_name": key,
                "source": declarations[key][1],
            }
            for key in sorted(declarations)
        ],
    }
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


__all__ = ["declaration_bytes", "load_dependency_state"]
