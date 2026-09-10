"""工作流作者源码可导入的静态标记（Authoring Markers）。

创作编译器只读取这些调用的 AST（抽象语法树），不会导入或执行作者源码。
这里的运行时对象仅让编辑器和类型检查器可以解析名称，不承担调度语义。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from unilabos.workflow.material_source import (
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


def group(*, name: str) -> AbstractContextManager[None]:
    """声明展示分组（Group）。

    参数说明：``name`` 是分组展示名；返回不承载执行屏障的静态上下文标记。
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("分组名称不能为空")
    return _AuthoringBlock()


def parallel() -> AbstractContextManager[None]:
    """声明源码并行结构（Parallel）；返回静态上下文标记。"""

    return _AuthoringBlock()


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


def material_source(
    *,
    resource_template: Any,
    mode: str,
    mount: Any,
    material_uuid: str | None,
    site: str | None,
    slot_range: list[str] | None,
    flow_role: MaterialFlowRole,
    custody_policy: MaterialCustodyPolicy | None = None,
    quantity_parameter: str | None = None,
    quantity_unit: str = "",
) -> Any:
    """声明只供创作编译的物料来源（MaterialSource）。

    参数说明：``resource_template`` 是显式导入的资源模板
    （ResourceTemplate）符号；``mode`` 是 ``existing | create_new``；
    ``mount`` 是 ``resource_ref`` 声明；``material_uuid`` 是可选固定物料
    （Material）身份；``site`` 是库位（Site）选择，``slot_range`` 是库位
    （Slot）范围；
    ``flow_role`` 是工作流局部物料流角色；``custody_policy`` 是可选的任务占用
    策略；``quantity_parameter`` 可绑定任务输入中的 lot 数量，
    ``quantity_unit`` 是可选单位。本标记没有返回值，被
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
        quantity_parameter,
        quantity_unit,
    )
    raise RuntimeError("工作流创作 material_source() 只能由静态编译器解析")


__all__ = [
    "DeviceSelector",
    "MATERIAL_FLOW_ROLE_LABELS_ZH",
    "MaterialCustodyPolicy",
    "MaterialFlowRole",
    "device",
    "group",
    "material_source",
    "parallel",
    "resource_ref",
    "workflow",
    "workflow_definition",
    "workflow_output",
]
