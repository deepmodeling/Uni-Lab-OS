"""工作流发布后的目录依赖创作刷新深模块（Deep Module）。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping


class CatalogAuthoringGenerationTracker:
    """封装工作流创作（Authoring）最后编译目录代际的进程内状态。"""

    def __init__(self) -> None:
        """建立没有历史目录基线的新进程追踪器。

        参数：无。
        返回：无；启动时不从数据库猜测旧进程使用的模板目录代际。
        异常：无。
        """

        # ``compiled_fingerprints`` 按工作流稳定身份保存本进程已确认完成的最后
        # 一次编译目录；它不是持久创作权威，重启后刻意为空。
        self._compiled_fingerprints: dict[str, str] = {}

    def requires_compile(
        self,
        workflow_uuid: str,
        catalog_fingerprint: str,
    ) -> bool:
        """判断来源是否尚未按当前模板目录完成编译。

        参数：``workflow_uuid`` 是工作流（Workflow）稳定身份；
        ``catalog_fingerprint`` 是当前模板目录代际指纹。
        返回：本进程没有该来源基线或指纹不同返回 ``True``，否则返回 ``False``。
        异常：无；指纹格式由工作流服务的目录权威读取器先行验证。
        """

        return self._compiled_fingerprints.get(workflow_uuid) != catalog_fingerprint

    def changed_from_known_generation(
        self,
        workflow_uuid: str,
        catalog_fingerprint: str,
    ) -> bool:
        """判断当前目录是否不同于本进程已知前代。

        参数：``workflow_uuid`` 是工作流稳定身份；``catalog_fingerprint`` 是当前
        模板目录指纹。返回：只有已知前代存在且不同才为 ``True``；启动恢复没有
        已知前代，不能伪报为目录变化。异常：无。
        """

        previous_fingerprint = self._compiled_fingerprints.get(workflow_uuid)
        return (
            previous_fingerprint is not None
            and previous_fingerprint != catalog_fingerprint
        )

    def record_compilation(
        self,
        workflow_uuid: str,
        catalog_fingerprint: str | None,
    ) -> None:
        """记录成功编译代际或清除不适用的来源基线。

        参数：``workflow_uuid`` 是工作流稳定身份；``catalog_fingerprint`` 是本次
        编译结果使用的目录指纹，源码缺失或未装配编译器时传 ``None``。
        返回：无；``None`` 会清除旧进程内记录，不创建虚假目录代际。
        异常：无。
        """

        if catalog_fingerprint is None:
            self._compiled_fingerprints.pop(workflow_uuid, None)
            return
        self._compiled_fingerprints[workflow_uuid] = catalog_fingerprint

    @staticmethod
    def source_signature(
        file_signature: tuple[object, ...],
        catalog_fingerprint: str | None,
    ) -> tuple[object, ...]:
        """组合文件身份与可选模板目录代际的监视签名。

        参数：``file_signature`` 是规范源码文件世代；``catalog_fingerprint`` 是
        已验证的当前模板目录指纹，未装配编译器时为 ``None``。
        返回：无目录时原样返回文件签名，否则在尾部附加目录标记和指纹。
        异常：无；不读取文件、数据库或编译器状态。
        """

        if catalog_fingerprint is None:
            return file_signature
        return (*file_signature, "catalog", catalog_fingerprint)


def refresh_catalog_dependent_authoring(
    *,
    dependent_workflow_uuids: Iterable[str],
    load_authoring: Callable[[str], Mapping[str, object]],
    reconcile_source: Callable[[str], Mapping[str, object]],
    apply_candidate: Callable[..., Mapping[str, object]],
    warnings: list[dict[str, str]],
    mutated_workflow_uuid: str,
) -> None:
    """重新编译并安全应用依赖刚更新实验操作的工作流。

    参数：``dependent_workflow_uuids`` 是直接引用子工作流新修订的父工作流稳定身份；
    ``load_authoring`` 在刷新前读取当前 Python 文件与持久记录合成的创作状态，
    ``reconcile_source`` 用当前目录重新编译父源码；``apply_candidate`` 通过公共
    Apply 入口应用服务端候选；
    ``warnings`` 收集实验操作提交后不可回滚的刷新问题；
    ``mutated_workflow_uuid`` 是刚应用的实验操作身份。返回：无；干净引用方源码的
    兼容候选会自动应用，已有未应用草稿只重新编译而不代替用户确认。异常：单项
    读取、编译或应用异常会被隔离成警告，绝不把已经提交的实验操作伪装成失败。
    """

    refreshed: set[str] = set()
    for dependent_workflow_uuid in dependent_workflow_uuids:
        dependent_workflow_uuid = str(dependent_workflow_uuid)
        if dependent_workflow_uuid == mutated_workflow_uuid:
            continue
        if dependent_workflow_uuid in refreshed:
            continue
        refreshed.add(dependent_workflow_uuid)
        try:
            before_refresh = load_authoring(dependent_workflow_uuid)
            # ``state=applied`` 由当前 Python 文件哈希、已应用来源、候选与诊断共同
            # 推导；不能只看持久记录，否则 IDE 刚写入但 watcher 尚未同步的草稿
            # 会被本轮目录刷新误当成干净来源并自动应用。
            parent_is_clean = before_refresh.get("state") == "applied"
            authoring = reconcile_source(dependent_workflow_uuid)
            candidate = authoring.get("candidate")
            if not parent_is_clean:
                if candidate is not None or authoring.get("state") != "applied":
                    _append_refresh_warning(warnings, dependent_workflow_uuid)
                continue
            if candidate is None:
                if authoring.get("state") != "applied":
                    _append_refresh_warning(warnings, dependent_workflow_uuid)
                continue
            if not isinstance(candidate, Mapping):
                raise TypeError("父工作流候选格式无效")
            candidate_hash = candidate.get("candidate_hash")
            if not isinstance(candidate_hash, str) or not candidate_hash:
                raise ValueError("父工作流候选缺少稳定哈希")
            result = apply_candidate(
                dependent_workflow_uuid,
                candidate_hash=candidate_hash,
            )
            apply_result = result.get("apply_result")
            nested_warnings = (
                apply_result.get("warnings")
                if isinstance(apply_result, Mapping)
                else None
            )
            if isinstance(nested_warnings, list):
                for warning in nested_warnings:
                    if isinstance(warning, dict) and warning not in warnings:
                        warnings.append(warning)
        except Exception:  # noqa: BLE001 - 主应用已提交，只能隔离派生刷新故障。
            _append_refresh_warning(warnings, dependent_workflow_uuid)


def _append_refresh_warning(
    warnings: list[dict[str, str]],
    workflow_uuid: str,
) -> None:
    """追加一条去重的父工作流待处理警告。

    参数：``warnings`` 是实验操作 Apply 结果中的可变警告集合；
    ``workflow_uuid`` 是未完成自动刷新的父工作流身份。返回：无；同一父工作流
    已存在相同警告时保持集合不变。异常：无。
    """

    warning = {
        "code": "dependent_authoring_refresh_pending",
        "message": (
            f"实验操作已更新，但引用方工作流 {workflow_uuid} 未能自动更新；"
            "请打开该工作流，检查组合节点参数和设备动作模板，重新编译并应用"
        ),
    }
    if warning not in warnings:
        warnings.append(warning)


__all__ = [
    "CatalogAuthoringGenerationTracker",
    "refresh_catalog_dependent_authoring",
]
