"""原子安装可编辑包（Editable Package）声明的工作流定义与源码事实。"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.models import validate_uuid

_REGISTRATION_FIELDS = (
    "workflow_uuid",
    "package_id",
    "package_root",
    "relative_path",
    "source_uri",
)
_PACKAGE_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SourceBootstrapConflict(RuntimeError):
    """表示整批工作流源码（Workflow Source）安装不能安全提交。"""


def _contains_path_control_character(value: str) -> bool:
    """判断持久路径身份是否包含 C0 或 DEL 控制字符。

    参数：``value`` 是待写入包根或源码相对路径的字符串。返回：存在
    U+0000..U+001F 或 U+007F 时为 ``True``，其余字符为 ``False``。异常：无；
    调用方已经在来源注册规范化阶段保证输入为字符串。
    """

    return any(ord(character) < 0x20 or character == "\x7f" for character in value)


def install_discovered_sources(
    conn: sqlite3.Connection,
    registrations: Iterable[Mapping[str, str]],
    *,
    now: str,
    before_commit: Callable[[], None] | None = None,
    allow_create_missing: bool = True,
) -> list[dict[str, Any]]:
    """在一个现有事务中安装完整源码发现（Source Discovery）计划。

    参数：``conn`` 是已经执行 ``BEGIN IMMEDIATE`` 的 ``workflow_history.db``
    连接；``registrations`` 是显式授权清单的完整、已发现来源集合；``now`` 是本次
    提交共享的 UTC 时间；``before_commit`` 在所有 SQL 写入后复核固定包根身份；
    ``allow_create_missing`` 只有显式源码发现安装入口设为 ``True``，旧兼容入口必须
    设为 ``False``。返回：按输入顺序排列的持久工作流源码（Workflow Source）注册行。
    异常：字段、身份、活动/软删除（Soft Deletion）生命周期或既有归属冲突时抛出
    ``SourceBootstrapConflict``；提交前复核异常原样传播，外层事务必须整体回滚。
    """

    # ``incoming`` 是完整批次的规范字符串快照，后续分类和写入不再读取调用者容器。
    incoming = _normalize_registrations(registrations)
    _validate_batch_identities(incoming)
    # ``existing_rows`` 是事务内全部持久来源归属，用于在任何新定义写入前预检。
    existing_rows = tuple(
        dict(row)
        for row in conn.execute("SELECT * FROM workflow_source_registration").fetchall()
    )
    _validate_existing_identities(incoming, existing_rows)
    # ``missing`` 只含从未存在的定义；活动定义复用，软删除（Soft Deletion）定义拒绝。
    missing = _classify_workflow_definitions(conn, incoming)
    if missing and not allow_create_missing:
        raise SourceBootstrapConflict("旧兼容入口不能创建缺失工作流定义")

    for registration in missing:
        _insert_workflow_skeleton(conn, registration=registration, now=now)
    # ``registered_workflow_uuids`` 避免幂等重启改写来源注册的创建/更新时间。
    registered_workflow_uuids = {row["workflow_uuid"] for row in existing_rows}
    for registration in incoming:
        workflow_uuid = registration["workflow_uuid"]
        if workflow_uuid not in registered_workflow_uuids:
            _insert_source_registration(conn, registration=registration, now=now)
        _ensure_empty_authoring(conn, workflow_uuid=workflow_uuid, now=now)
    if before_commit is not None:
        before_commit()
    return [
        _read_registration(conn, registration["workflow_uuid"])
        for registration in incoming
    ]


def _normalize_registrations(
    registrations: Iterable[Mapping[str, str]],
) -> tuple[dict[str, str], ...]:
    """冻结并验证完整来源注册字段。

    参数：``registrations`` 是调用者提供的来源映射集合。返回：只含五个规范字段的
    独立字典元组。异常：集合不可迭代、字段缺失、值非字符串/为空或来源 URI 与
    包身份不一致时抛出 ``SourceBootstrapConflict``。
    """

    try:
        incoming_rows = tuple(registrations)
    except (KeyError, TypeError):
        raise SourceBootstrapConflict("工作流源码注册字段不完整") from None
    if any(
        not isinstance(registration, Mapping)
        or set(registration) != set(_REGISTRATION_FIELDS)
        for registration in incoming_rows
    ):
        raise SourceBootstrapConflict("工作流源码注册字段不完整")
    normalized = tuple(
        {field: registration[field] for field in _REGISTRATION_FIELDS}
        for registration in incoming_rows
    )
    for registration in normalized:
        if any(
            not isinstance(registration[field], str) or not registration[field]
            for field in _REGISTRATION_FIELDS
        ):
            raise SourceBootstrapConflict("工作流源码注册字段无效")
        try:
            # ``canonical_workflow_uuid`` 是不可由宽松字符串改写得到的规范工作流身份。
            canonical_workflow_uuid = validate_uuid(registration["workflow_uuid"])
        except (TypeError, ValueError):
            raise SourceBootstrapConflict("工作流 UUID 不是规范非空身份") from None
        if canonical_workflow_uuid != registration["workflow_uuid"]:
            raise SourceBootstrapConflict("工作流 UUID 不是规范非空身份")
        if _PACKAGE_ID.fullmatch(registration["package_id"]) is None:
            raise SourceBootstrapConflict("可编辑包身份不符合规范")
        _validate_package_root(registration["package_root"])
        _validate_relative_source_path(registration["relative_path"])
        # ``expected_source_uri`` 由包身份与相对路径唯一确定，调用者不能注入别名。
        expected_source_uri = (
            f"package://{registration['package_id']}/{registration['relative_path']}"
        )
        if registration["source_uri"] != expected_source_uri:
            raise SourceBootstrapConflict("工作流源码 URI 与清单身份不一致")
    return normalized


def _validate_package_root(package_root: str) -> None:
    """验证持久包根是规范 POSIX 或 Windows 绝对路径。

    参数：``package_root`` 是工作流源码（Workflow Source）的包目录身份。返回：合法
    时无返回值。异常：相对路径、控制字符、父级穿越、文件系统根、Windows 设备命名
    空间或需规范化改写时抛出 ``SourceBootstrapConflict``；本函数只验证身份形状，
    不授予文件系统权限。
    """

    # 两种 ``PurePath`` 都只做词法验证；实际目录授权仍由源码发现阶段固定。
    posix_path = PurePosixPath(package_root)
    windows_path = PureWindowsPath(package_root)
    canonical_posix = (
        "\\" not in package_root
        and posix_path.is_absolute()
        and package_root == posix_path.as_posix()
        and ".." not in posix_path.parts
        and len(posix_path.parts) >= 2
    )
    windows_drive = windows_path.drive
    canonical_windows_drive = re.fullmatch(r"[A-Za-z]:", windows_drive) is not None
    canonical_windows_unc = windows_drive.startswith(
        "\\\\"
    ) and not windows_drive.startswith(("\\\\?\\", "\\\\.\\"))
    canonical_windows_components = all(
        not any(character in '<>:"|?*' for character in component)
        and not component.endswith((" ", "."))
        for component in windows_path.parts[1:]
    )
    canonical_windows = (
        "/" not in package_root
        and windows_path.is_absolute()
        and package_root == str(windows_path)
        and ".." not in windows_path.parts
        and len(windows_path.parts) >= 2
        and (canonical_windows_drive or canonical_windows_unc)
        and canonical_windows_components
    )
    if _contains_path_control_character(package_root) or not (
        canonical_posix or canonical_windows
    ):
        raise SourceBootstrapConflict("可编辑包根目录身份无效")


def _validate_relative_source_path(relative_path: str) -> None:
    """验证持久源码路径严格位于 ``workflows/*.py``。

    参数：``relative_path`` 是包根内的工作流源码（Workflow Source）位置。返回：
    合法时无返回值。异常：绝对路径、穿越、嵌套、反斜线、控制字符或非 Python
    文件时抛出 ``SourceBootstrapConflict``。
    """

    # ``source_path`` 是不依赖当前工作目录的纯 POSIX 相对身份。
    source_path = PurePosixPath(relative_path)
    if (
        _contains_path_control_character(relative_path)
        or "\\" in relative_path
        or source_path.is_absolute()
        or relative_path != source_path.as_posix()
        or len(source_path.parts) != 2
        or source_path.parts[0] != "workflows"
        or source_path.suffix != ".py"
        or not source_path.stem
    ):
        raise SourceBootstrapConflict("工作流源码相对路径无效")


def _validate_batch_identities(
    registrations: tuple[dict[str, str], ...],
) -> None:
    """在写库前验证批内工作流、路径、URI 与包根身份唯一。

    参数：``registrations`` 是规范完整批次。返回：全部身份互不冲突时无返回值。
    异常：任一重复归属或同包多根时抛出 ``SourceBootstrapConflict``。
    """

    workflow_uuids: set[str] = set()
    physical_paths: set[tuple[str, str]] = set()
    source_uris: set[str] = set()
    package_roots: dict[str, str] = {}
    for registration in registrations:
        # ``physical_path`` 是包根与相对源码路径组成的实际文件身份。
        physical_path = (
            registration["package_root"],
            registration["relative_path"],
        )
        prior_root = package_roots.setdefault(
            registration["package_id"], registration["package_root"]
        )
        if (
            registration["workflow_uuid"] in workflow_uuids
            or physical_path in physical_paths
            or registration["source_uri"] in source_uris
            or prior_root != registration["package_root"]
        ):
            raise SourceBootstrapConflict("批次内工作流源码身份重复")
        workflow_uuids.add(registration["workflow_uuid"])
        physical_paths.add(physical_path)
        source_uris.add(registration["source_uri"])


def _validate_existing_identities(
    registrations: tuple[dict[str, str], ...],
    existing_rows: tuple[dict[str, Any], ...],
) -> None:
    """验证新批次不会重绑定任何既有工作流源码事实。

    参数：``registrations`` 是待安装完整批次；``existing_rows`` 是事务内既有注册
    快照。返回：全部归属相容时无返回值。异常：工作流、文件、URI 或包目录发生
    重绑定时抛出 ``SourceBootstrapConflict``。
    """

    existing_by_workflow = {row["workflow_uuid"]: row for row in existing_rows}
    physical_owners = {
        (row["package_root"], row["relative_path"]): row["workflow_uuid"]
        for row in existing_rows
    }
    uri_owners = {row["source_uri"]: row["workflow_uuid"] for row in existing_rows}
    package_roots: dict[str, str] = {}
    for row in existing_rows:
        prior_root = package_roots.setdefault(row["package_id"], row["package_root"])
        if prior_root != row["package_root"]:
            raise SourceBootstrapConflict("既有包身份指向多个目录")

    for registration in registrations:
        # ``workflow_uuid`` 是当前来源希望稳定绑定的工作流（Workflow）身份。
        workflow_uuid = registration["workflow_uuid"]
        current = existing_by_workflow.get(workflow_uuid)
        if current is not None and any(
            current[field] != registration[field]
            for field in (
                "package_id",
                "package_root",
                "relative_path",
                "source_uri",
            )
        ):
            raise SourceBootstrapConflict("工作流源码身份不能在启动时重绑定")
        # ``physical_identity`` 是本项希望取得的实际源码文件身份。
        physical_identity = (
            registration["package_root"],
            registration["relative_path"],
        )
        if physical_owners.get(physical_identity, workflow_uuid) != workflow_uuid:
            raise SourceBootstrapConflict("工作流源码物理路径已被占用")
        if uri_owners.get(registration["source_uri"], workflow_uuid) != workflow_uuid:
            raise SourceBootstrapConflict("工作流源码 URI 已被占用")
        prior_root = package_roots.setdefault(
            registration["package_id"], registration["package_root"]
        )
        if prior_root != registration["package_root"]:
            raise SourceBootstrapConflict("包身份不能指向多个目录")


def _classify_workflow_definitions(
    conn: sqlite3.Connection,
    registrations: tuple[dict[str, str], ...],
) -> tuple[dict[str, str], ...]:
    """把清单身份分类为活动、缺失或软删除（Soft Deletion）工作流定义。

    参数：``conn`` 是当前写事务；``registrations`` 是已通过全部身份预检的批次。
    返回：仅包含缺失定义的注册元组。异常：任一身份存在软删除历史时抛出
    ``SourceBootstrapConflict``，绝不自动复活。
    """

    missing: list[dict[str, str]] = []
    for registration in registrations:
        # ``workflow_uuid`` 是必须在活动、缺失和软删除历史之间唯一分类的定义身份。
        workflow_uuid = registration["workflow_uuid"]
        # ``definition_row`` 同时读取活动和软删除行，避免普通活动查询掩盖历史身份。
        definition_row = conn.execute(
            "SELECT uuid, deleted_at FROM workflow WHERE uuid = ?",
            (workflow_uuid,),
        ).fetchone()
        if definition_row is None:
            missing.append(registration)
        elif definition_row["deleted_at"] is not None:
            raise SourceBootstrapConflict("软删除工作流定义不能由源码清单复活")
    return tuple(missing)


def _insert_workflow_skeleton(
    conn: sqlite3.Connection,
    *,
    registration: Mapping[str, str],
    now: str,
) -> None:
    """为一项缺失清单身份创建最小后端形态（Backend-shaped）工作流骨架。

    参数：``conn`` 是当前事务；``registration`` 是缺失定义对应的工作流源码注册；
    ``now`` 是批次统一时间。返回：无；只写定义与清单来源追溯，不创建图、任务、
    候选或源码文件。
    """

    # ``source_stem`` 与包身份组成首次稳定名称，不读取或执行不可信 Python 源码。
    source_stem = PurePosixPath(registration["relative_path"]).stem
    workflow_name = f"{registration['package_id']}.{source_stem}"
    # ``manifest_provenance`` 只记录稳定清单来源坐标，供后续编译诊断和身份审计使用。
    manifest_provenance = {
        "unilab": {
            "source_bootstrap": {
                "kind": "editable_package_manifest",
                "package_id": registration["package_id"],
                "relative_path": registration["relative_path"],
                "source_uri": registration["source_uri"],
            }
        }
    }
    conn.execute(
        """
        INSERT INTO workflow(
            uuid, create_time, update_time, deleted_at,
            description, meta_data, name, tags, revision
        ) VALUES (?, ?, ?, NULL, NULL, ?, ?, '[]', 1)
        """,
        (
            registration["workflow_uuid"],
            now,
            now,
            encode_json(manifest_provenance, sort_keys=True).decode("utf-8"),
            workflow_name,
        ),
    )


def _insert_source_registration(
    conn: sqlite3.Connection,
    *,
    registration: Mapping[str, str],
    now: str,
) -> None:
    """插入一项首次出现的工作流源码注册。

    参数：``conn`` 是当前事务；``registration`` 是已验证且无既有归属的来源；
    ``now`` 是统一时间。返回：无；唯一约束异常交给外层映射并回滚。
    """

    conn.execute(
        """
        INSERT INTO workflow_source_registration(
            workflow_uuid, package_id, package_root,
            relative_path, source_uri, create_time, update_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            registration["workflow_uuid"],
            registration["package_id"],
            registration["package_root"],
            registration["relative_path"],
            registration["source_uri"],
            now,
            now,
        ),
    )


def _ensure_empty_authoring(
    conn: sqlite3.Connection,
    *,
    workflow_uuid: str,
    now: str,
) -> None:
    """为已安装来源补充空工作流创作（Authoring）事实。

    参数：``conn`` 是当前事务；``workflow_uuid`` 是定义稳定身份；``now`` 是统一
    时间。返回：无；既有创作记录保持原样，不触发编译、应用或任务创建。
    """

    conn.execute(
        """
        INSERT INTO workflow_authoring(workflow_uuid, diagnostics, update_time)
        VALUES (?, '[]', ?)
        ON CONFLICT(workflow_uuid) DO NOTHING
        """,
        (workflow_uuid, now),
    )


def _read_registration(conn: sqlite3.Connection, workflow_uuid: str) -> dict[str, Any]:
    """在同一事务中读取刚确认的来源注册。

    参数：``conn`` 是当前事务；``workflow_uuid`` 是工作流（Workflow）身份。
    返回：完整持久注册字典。异常：不变量破坏导致行缺失时抛出
    ``SourceBootstrapConflict``。
    """

    row = conn.execute(
        "SELECT * FROM workflow_source_registration WHERE workflow_uuid = ?",
        (workflow_uuid,),
    ).fetchone()
    if row is None:
        raise SourceBootstrapConflict("工作流源码注册提交前不可见")
    return dict(row)
