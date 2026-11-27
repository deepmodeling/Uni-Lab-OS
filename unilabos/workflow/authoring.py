"""工作流作者源码可导入的静态标记（Authoring Markers）。

创作编译器只读取这些调用的 AST（抽象语法树），不会导入或执行作者源码。
这里的运行时对象仅让编辑器和类型检查器可以解析名称，不承担调度语义。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TypeVar, overload

from unilabos.workflow.material_source import (
    MATERIAL_CUSTODY_POLICY_LABELS_ZH,
    MATERIAL_FLOW_ROLE_LABELS_ZH,
    MaterialCustodyPolicy,
    MaterialFlowRole,
)

_Function = TypeVar("_Function", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class DeviceSelector:
    """作者源码中的设备选择声明，仅保存可选固定设备身份。"""

    device_id: str | None = None

    def __getattr__(self, action_name: str) -> Callable[..., Any]:
        """为编辑器返回一个不可执行的动作占位调用。

        参数说明：``action_name`` 是作者访问的动作业务名；返回函数一旦被真正
        调用就抛出错误，防止误把作者标记当成设备执行接口。
        """

        def unavailable_action(*_args: Any, **_kwargs: Any) -> Any:
            """拒绝在 Python 运行时执行创作动作；参数仅用于兼容调用形状。"""

            raise RuntimeError("工作流作者动作只能由创作编译器静态解析")

        unavailable_action.__name__ = action_name
        return unavailable_action


@dataclass(frozen=True, slots=True)
class _AuthoringBlock(AbstractContextManager[None]):
    """仅供类型检查使用的 group/parallel 上下文标记。"""

    def __enter__(self) -> None:
        """进入静态创作块；运行时不产生领域状态。"""

        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> bool:
        """退出静态创作块。

        参数说明：三个参数是上下文管理器异常信息；始终返回 ``False``，不吞掉
        运行时异常。
        """

        del exc_type, exc_value, traceback
        return False


@dataclass(frozen=True, slots=True)
class _ResourceBlock(AbstractContextManager[None]):
    """仅供类型检查使用的结构化资源作用域标记。"""

    resource_ids: tuple[str, ...]

    def __enter__(self) -> None:
        """进入静态资源范围；资源取得由编译计划负责。"""

        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> bool:
        """退出资源范围且不吞掉作者源码运行时异常。"""

        del exc_type, exc_value, traceback
        return False


@dataclass(frozen=True, slots=True)
class RepeatUntilBinding(AbstractContextManager["RepeatUntilBinding"]):
    """仅为编辑器描述 ``repeat_until`` 的 carry/next 创作接口。"""

    carry: Mapping[str, Any]

    def __enter__(self) -> "RepeatUntilBinding":
        """返回静态循环绑定；真实循环只由 AST 编译器与调度器执行。"""

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> bool:
        """退出静态循环块且不吞掉异常。"""

        del exc_type, exc_value, traceback
        return False

    def next(self, **values: Any) -> None:
        """声明下一轮 carry；运行时执行作者源码时始终失败关闭。"""

        del values
        raise RuntimeError("工作流创作 loop.next() 只能由静态编译器解析")


def workflow(**metadata: Any) -> Callable[[_Function], _Function]:
    """声明工作流定义（Workflow Definition）的规范静态装饰器。

    参数说明：``metadata`` 包含工作流 UUID、展示名和描述；返回保持原函数不变
    的装饰器。可信编译只读取 AST，不信任这里的运行结果。
    """

    del metadata

    def decorate(function: _Function) -> _Function:
        """返回原作者函数；``function`` 仅供编辑器和类型检查器使用。"""

        return function

    return decorate


def workflow_definition(**metadata: Any) -> Callable[[_Function], _Function]:
    """兼容旧作者草稿中的工作流定义装饰器。

    参数说明：``metadata`` 与规范 ``workflow`` 完全相同；返回规范装饰器结果。
    可信编译器接受该旧名称，但确定性源码只生成 ``@workflow``。
    """

    return workflow(**metadata)


def device(device_id: str | None = None) -> DeviceSelector:
    """声明一个设备选择器（Device Selector）。

    参数说明：``device_id`` 缺失表示运行时分配，非空字符串表示固定设备；返回
    仅供静态创作的选择器对象。
    """

    if device_id is not None and (not isinstance(device_id, str) or not device_id):
        raise ValueError("固定设备身份必须是非空字符串")
    return DeviceSelector(device_id)


def service() -> DeviceSelector:
    """声明 OS 本地服务选择器；没有设备实例或物理派发。"""
    return DeviceSelector(None)


def group(*, name: str) -> AbstractContextManager[None]:
    """声明展示分组（Group）。

    参数说明：``name`` 是分组展示名；返回不承载执行屏障的静态上下文标记。
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("分组名称不能为空")
    return _AuthoringBlock()


def parallel(*, depends_on: list[Any] | None = None) -> AbstractContextManager[None]:
    """声明并行结构；depends_on 覆盖块的隐式前序，省略时继承前沿。"""

    return _AuthoringBlock()


class _ResourceHolds(AbstractContextManager["_ResourceHolds"]):
    """静态预留声明块；进入此块不会取得资源。"""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def hold(self, *resource_ids: str, nodes: list[Any], name: str = "") -> None:
        """在所有选中动作结束前保留指定资源；site:<设备>/<库位> 绑定库存库位。"""
        _resource_names(resource_ids)
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("预留的 nodes 必须为非空列表")
        if not isinstance(name, str):
            raise ValueError("预留名称必须为字符串")


def _resource_names(resource_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not resource_ids or any(
        not isinstance(item, str) or not item.strip() for item in resource_ids
    ):
        raise ValueError("资源声明必须包含非空字符串别名")
    normalized = tuple(item.strip() for item in resource_ids)
    if len(set(normalized)) != len(normalized):
        raise ValueError("资源声明不能包含重复别名")
    return normalized


@overload
def resources() -> _ResourceHolds: ...


@overload
def resources(resource_id: str, /, *resource_ids: str) -> AbstractContextManager[None]: ...


def resources(*resource_ids: str) -> AbstractContextManager[None] | _ResourceHolds:
    """with resources("设备") 声明词法范围；with resources() as locks 仅收集 locks.hold 声明。"""
    if not resource_ids:
        return _ResourceHolds()
    return _ResourceBlock(_resource_names(resource_ids))


def repeat_until(
    *,
    max_iterations: int,
    carry: Mapping[str, Any],
) -> RepeatUntilBinding:
    """声明先执行一轮、再判断退出的调度器本地循环区域。"""

    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise ValueError("max_iterations 必须是正整数")
    if max_iterations < 1:
        raise ValueError("max_iterations 必须是正整数")
    if not isinstance(carry, Mapping):
        raise ValueError("carry 必须是映射")
    return RepeatUntilBinding(dict(carry))


def until(condition: bool) -> None:
    """声明 ``repeat_until`` 的严格布尔退出条件。"""

    del condition
    raise RuntimeError("工作流创作 until() 只能由静态编译器解析")


def workflow_output(**outputs: Any) -> dict[str, Any]:
    """声明工作流输出（Workflow Output）。

    参数说明：``outputs`` 把输出名绑定到工作流输入或节点结果；返回浅拷贝仅供
    编辑器体验，可信编译器静态读取关键字参数。
    """

    return dict(outputs)


def resource_ref(resource_id: str) -> Any:
    """声明只供创作编译的稳定资源引用。

    参数说明：``resource_id`` 是作者明确给出的实例 UUID；本运行时
    标记没有返回值，被执行时抛出 ``RuntimeError``，只允许可信静态
    编译器从 AST（抽象语法树）解析。
    """

    del resource_id
    raise RuntimeError("工作流创作 resource_ref() 只能由静态编译器解析")


def site_group(group_key: str, *, exact: str | None = None) -> Any:
    """声明动作目标库位使用部署定义的命名库位组。

    参数：``group_key`` 是同一父资源内由部署配置维护的稳定业务组键；``exact``
    可绑定一个工作流运行参数，非空时强制选择组内该库位。返回：本标记不产生
    运行时值；可信编译器把它冻结为逻辑库位选择器，Task 创建时再解析该部署
    代际的具体候选 UUID。异常：若作者源码被直接执行则抛出 ``RuntimeError``，
    禁止绕过库存权威自行选择库位。
    """

    del group_key, exact
    raise RuntimeError("工作流创作 site_group() 只能由静态编译器解析")


def material_source(
    *,
    resource_template: Any,
    mode: str,
    mount: Any,
    material_uuid: str | None,
    site: str | None,
    slot_range: list[str] | None,
    flow_role: MaterialFlowRole,
    custody_policy: MaterialCustodyPolicy = MaterialCustodyPolicy.TASK_EXCLUSIVE,
    supports_replenishment: bool = False,
    search_descendants: bool = False,
) -> Any:
    """声明只供创作编译的物料来源（MaterialSource）。

    参数说明：``resource_template`` 是显式导入的资源模板
    （ResourceTemplate）符号；``mode`` 是 ``existing | create_new``；
    ``mount`` 是 ``resource_ref`` 声明；``material_uuid`` 是可选固定物料
    （Material）身份；``site`` 是固定库位 UUID，或直接引用字符串工作流输入，
    后者在启动时按挂载物料解析库位名并冻结 UUID；``slot_range`` 是库位
    （Slot）范围，也可限制启动库位参数的允许范围；``flow_role`` 是工作流局部物料流角色，
    ``custody_policy`` 是物料保管策略（MaterialCustodyPolicy）；旧调用缺省时
    显式迁移为 ``TASK_EXCLUSIVE``，保持升级前的安全占用语义。
    ``search_descendants=True`` 仅用于 existing 自动选料：按各层 Site 顺序遍历
    挂载物料的实际占位子树；site/slot_range 限制根层子树。选择与原有整组任务
    准入、预留共用事务，且仍发生在首次动作派发前；不会触发物理搬运。
    本标记没有返回值，被
    执行时抛出 ``RuntimeError``，防止越过创作编译边界读写物料权威。
    """

    del (
        resource_template,
        mode,
        mount,
        material_uuid,
        site,
        slot_range,
        flow_role,
        custody_policy,
        search_descendants,
    )
    raise RuntimeError("工作流创作 material_source() 只能由静态编译器解析")


def material_io(
    *,
    mode: str,
    resource_template: Any | None,
    mount: Any,
    site: str | None,
    site_uuid: str | None = None,
    material: Any | None,
    disposition: str | None,
    scope: str | None,
    timeout_seconds: int,
    reagent: Any | None = None,
    name: str | None = None,
    barcode: str | None = None,
) -> Any:
    """声明由框架协调、经人工确认放行的物料装卸边界。

    ``load`` 创建模板物料并输出 ``material``；可选 ``name``、``barcode``
    写入新物料身份，可选 ``reagent`` 在同一确认步骤创建或复用试剂身份并挂试剂。
    ``unload`` 接收上游物料或 ``resource_ref``。``refill`` 对已占位容器只加
    试剂余量，不改占用、不新建身份，``reagent`` 仅含 ``quantity`` 与
    ``quantity_unit``。``load`` 的 ``site`` 是库位名（例如 ``T12``），
    ``site_uuid`` 是可选 UUID。本标记只供静态编译，不派发设备动作。
    """

    del (
        mode,
        resource_template,
        mount,
        site,
        site_uuid,
        material,
        disposition,
        scope,
        timeout_seconds,
        reagent,
        name,
        barcode,
    )
    raise RuntimeError("工作流创作 material_io() 只能由静态编译器解析")


def quantity_requirement(
    *,
    requirement_key: str,
    source: Any,
    consume: Any,
    quantity: float,
    quantity_unit: str,
    scale: float = 1.0,
    description: str | None = None,
) -> None:
    """声明一个来源容器中试剂或当前内容物的任务级数量需求。

    ``source`` 必须直接引用 ``material_source`` 的结果，``consume`` 必须直接
    引用实际消耗该内容物的动作结果。``quantity`` 可以是有限正数字面量或工作流
    输入；编译器先乘 ``scale``，再以 ``quantity_unit`` 在 Task 准入时预占。
    动态输入解析为零时本次任务不产生该需求。该标记仅供 AST 编译，不能执行。
    """

    del requirement_key, source, consume, quantity, quantity_unit, scale, description
    raise RuntimeError("工作流创作 quantity_requirement() 只能由静态编译器解析")


__all__ = [
    "DeviceSelector",
    "service",
    "MATERIAL_CUSTODY_POLICY_LABELS_ZH",
    "MATERIAL_FLOW_ROLE_LABELS_ZH",
    "MaterialCustodyPolicy",
    "MaterialFlowRole",
    "RepeatUntilBinding",
    "device",
    "group",
    "material_io",
    "material_source",
    "parallel",
    "quantity_requirement",
    "repeat_until",
    "resource_ref",
    "resources",
    "site_group",
    "workflow",
    "workflow_definition",
    "workflow_output",
    "until",
]
