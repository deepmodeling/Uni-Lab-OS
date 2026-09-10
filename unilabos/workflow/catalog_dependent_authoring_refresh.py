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
    registrations: Iterable[Mapping[str, object]],
    load_authoring_record: Callable[[str], Mapping[str, object]],
    reconcile_source: Callable[[str], object],
    warnings: list[dict[str, str]],
    mutated_workflow_uuid: str,
) -> None:
    """刷新可能依赖刚发布目录合同的工作流创作派生状态。

    参数：``registrations`` 是同一活动软件包目录（Package Catalog）代际的来源
    登记；``load_authoring_record`` 按工作流 UUID 读取创作记录；
    ``reconcile_source`` 强制重编译一个活动工作流源码（Workflow Source）；
    ``warnings`` 收集主应用提交后不可回滚的刷新警告；
    ``mutated_workflow_uuid`` 是刚发布的工作流（Workflow）身份。
    返回：无；只刷新具有候选版本（Candidate）或诊断的其他活动来源。
    异常：单项读取或重编译异常会被隔离并转成警告，绝不把已提交发布伪装成
    失败。
    """

    for registration in registrations:
        # ``dependent_workflow_uuid`` 是可能引用新发布合同的活动工作流身份。
        dependent_workflow_uuid = str(registration["workflow_uuid"])
        if dependent_workflow_uuid == mutated_workflow_uuid:
            continue
        try:
            # ``authoring_record`` 中候选和诊断会随模板目录变化而失效。
            authoring_record = load_authoring_record(dependent_workflow_uuid)
            if (
                authoring_record.get("candidate") is None
                and not authoring_record.get("diagnostics")
            ):
                continue
            reconcile_source(dependent_workflow_uuid)
        except Exception:  # noqa: BLE001 - 主应用已提交，只能隔离派生刷新故障。
            warnings.append(
                {
                    "code": "dependent_authoring_refresh_pending",
                    "message": "工作流已应用，但依赖创作草稿仍待重新编译",
                }
            )


__all__ = [
    "CatalogAuthoringGenerationTracker",
    "refresh_catalog_dependent_authoring",
]
