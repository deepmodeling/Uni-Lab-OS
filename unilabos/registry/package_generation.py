"""管理软件包定义在实时注册表（Registry）中的原子发布代际。"""

from __future__ import annotations

import copy
import threading
from typing import Any, Literal


class PackageRegistryGeneration:
    """封装软件包注册表快照（Registry Snapshot）的发布与身份解析。"""

    def __init__(self, registry: Any) -> None:
        """建立一个依附实时注册表（Registry）的软件包发布代际。

        参数：``registry`` 是持有设备与资源定义映射的实时注册表实例。
        返回：无；实例初始化独立重入锁，且尚未发布软件包注册表快照
        （Registry Snapshot）。
        异常：无；注册表映射形状在首次发布或解析时关闭式校验。
        """

        # ``_registry`` 是被本模块统一推进设备、资源和快照代际的实时注册表。
        self._registry = registry
        # ``_lock`` 保证候选构造、整体发布与身份解析观察同一权威代际。
        self._lock = threading.RLock()
        # ``_snapshot`` 是当前已发布的完整软件包注册表快照，不导入作者模块。
        self._snapshot: Any = None

    def publish(self, snapshot: Any) -> None:
        """原子发布一代完整软件包注册表快照（Registry Snapshot）。

        参数：``snapshot`` 是已完整编译和全局校验的软件包注册表快照，必须提供
        ``registry_candidates`` 候选构造接口。
        返回：无；成功后设备、资源定义映射和当前快照在同一代际内整体前进。
        异常：快照接口无效、定义冲突、候选形状非法或候选构造失败时传播异常；
        实时注册表保持原代际，不发布部分设备或资源定义。
        """

        # ``candidate_builder`` 负责从完整旧代际构造两个不共享容器的新映射。
        candidate_builder = getattr(snapshot, "registry_candidates", None)
        if not callable(candidate_builder):
            raise TypeError("软件包注册表快照缺少 registry_candidates 接口")
        with self._lock:
            # 候选必须在锁内读取同一旧代际；构造失败前不会触碰实时权威映射。
            candidate_devices, candidate_resources = candidate_builder(
                self._registry.device_type_registry,
                self._registry.resource_type_registry,
            )
            if not isinstance(candidate_devices, dict) or not isinstance(
                candidate_resources,
                dict,
            ):
                raise TypeError("软件包注册表候选必须是设备和资源字典")
            # 三项共同构成新权威代际；所有正式读取都通过同一锁内的解析接口。
            (
                self._registry.device_type_registry,
                self._registry.resource_type_registry,
                self._snapshot,
            ) = (candidate_devices, candidate_resources, snapshot)

    def resolve(
        self,
        kind: Literal["device", "resource"],
        identity: str,
    ) -> dict[str, Any]:
        """解析当前代际中的设备或资源定义身份。

        参数：``kind`` 是 ``device`` 或 ``resource``；``identity`` 是现有精确
        注册表 key、软件包规范全限定身份（FQID）或全代唯一兼容短名。
        返回：当前实时注册表权威代际中的定义条目；短名不会产生别名行。
        异常：种类无效、身份为空、定义不存在、短名歧义，或快照与实时映射不
        一致时关闭式抛出异常。
        """

        if kind not in {"device", "resource"}:
            raise ValueError(f"不支持的注册表定义种类: {kind}")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("注册表定义身份不能为空")
        # ``normalized_identity`` 是去除输入边界空白后的待解析稳定身份。
        normalized_identity = identity.strip()
        with self._lock:
            # ``live_definitions`` 是当前权威代际中指定种类的实时定义映射。
            live_definitions = (
                self._registry.device_type_registry
                if kind == "device"
                else self._registry.resource_type_registry
            )
            exact_entry = live_definitions.get(normalized_identity)
            if exact_entry is not None:
                return exact_entry
            if self._snapshot is None:
                raise KeyError(f"{kind} 注册表定义不存在: {normalized_identity}")
            # ``package_definition`` 只把唯一短名解析到规范 FQID；最终条目仍从
            # 同一代际的实时映射读取，防止静态目录成为第二权威事实源。
            package_definition = self._snapshot.resolve(kind, normalized_identity)
            live_entry = live_definitions.get(package_definition.fqid)
            if live_entry is None:
                raise RuntimeError(
                    f"软件包注册表快照与实时定义映射不一致: {package_definition.fqid}"
                )
            return live_entry

    def snapshot_projection(self) -> dict[str, Any]:
        """查询当前完整包目录代的隔离注册表快照（Registry Snapshot）投影。

        参数：无。
        返回：包含主包和外部包设备、资源、显式工作流（Workflow）与资产的全新
        JSON 字典；调用方修改任何层级都不会改变当前注册表权威代际。
        异常：尚未发布快照、快照缺少 ``to_dict`` 接口或返回值不是字典时抛出
        ``RuntimeError``/``TypeError``，不会从实时映射猜测不完整投影。
        """

        with self._lock:
            if self._snapshot is None:
                raise RuntimeError("软件包注册表快照尚未发布")
            projection_builder = getattr(self._snapshot, "to_dict", None)
            if not callable(projection_builder):
                raise TypeError("软件包注册表快照缺少 to_dict 查询接口")
            # ``snapshot_projection`` 是从同一锁内当前代生成的调用方独占查询对象。
            snapshot_projection = projection_builder()
            if not isinstance(snapshot_projection, dict):
                raise TypeError("软件包注册表快照查询投影必须是字典")
            return copy.deepcopy(snapshot_projection)
