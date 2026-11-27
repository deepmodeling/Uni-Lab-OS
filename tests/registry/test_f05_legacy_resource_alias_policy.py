"""F05 资源模板（ResourceTemplate）遗留实现类别名发布策略。"""

from __future__ import annotations

from unilabos.registry.template_projection import (
    compile_resource_template_source_aliases,
)


def _resource_definition(
    business_id: str,
    class_module: str,
    *,
    source_fqid: str | None = None,
) -> dict[str, object]:
    """构造仅包含来源身份决策字段的资源模板定义。

    参数说明：``business_id`` 是注册表（Registry）业务 ID；``class_module``
    是可被多个模板复用的 Python 实现类身份；``source_fqid`` 是作者显式声明的
    稳定源码身份。返回：可交给产品来源别名编译器的资源模板
    （ResourceTemplate）定义；本辅助函数不创建库存（Inventory）事实。
    """

    # ``definition`` 保留业务身份、实现身份以及可选的显式源码身份。
    definition: dict[str, object] = {
        "id": business_id,
        "class": {"module": class_module},
    }
    if source_fqid is not None:
        definition["source_fqid"] = source_fqid
    return definition


def test_explicit_and_legacy_shared_class_publish_no_compatibility_alias() -> None:
    """显式模板与遗留模板共享实现类时不得发布兼容别名。

    参数：无。返回：无；断言显式 ``source_fqid`` 仍可解析，而共享
    ``class.module`` 因拥有两个资源模板（ResourceTemplate）所有者而不进入
    来源映射，调用方不能把它解析成其中任一业务模板。
    """

    # ``shared_class`` 是显式模板与遗留模板共同复用的实现类，不是业务身份。
    shared_class = "lab.resources.shared:Container"
    # ``source_aliases`` 是产品编译器允许发布到工作流创作目录的来源映射。
    source_aliases = compile_resource_template_source_aliases(
        [
            _resource_definition(
                "explicit_plate",
                shared_class,
                source_fqid="lab.resources:explicit_plate",
            ),
            _resource_definition("legacy_plate", shared_class),
        ]
    )

    assert source_aliases == {"lab.resources:explicit_plate": "explicit_plate"}
    assert shared_class not in source_aliases


def test_two_legacy_templates_sharing_class_publish_no_compatibility_alias() -> None:
    """两个遗留模板共享实现类时不得猜测任一来源身份。

    参数：无。返回：无；断言全代际存在两个 ``class.module`` 所有者时不发布
    兼容来源别名，但两个注册表（Registry）业务 ID 仍可由上层分别同步。
    """

    # ``shared_class`` 是两个旧 YAML 资源模板共同复用的容器实现类。
    shared_class = "lab.resources.shared:LegacyContainer"
    # ``source_aliases`` 只包含能一一证明所有者的来源身份。
    source_aliases = compile_resource_template_source_aliases(
        [
            _resource_definition("legacy_plate_a", shared_class),
            _resource_definition("legacy_plate_b", shared_class),
        ]
    )

    assert source_aliases == {}
    assert shared_class not in source_aliases


def test_unique_legacy_template_keeps_compatibility_alias() -> None:
    """全代际唯一的遗留实现类仍须保留兼容解析能力。

    参数：无。返回：无；断言恰好一个无显式 ``source_fqid`` 的资源模板
    （ResourceTemplate）拥有某实现类时，``class.module`` 稳定解析到该业务 ID。
    """

    # ``unique_class`` 是当前代际只有一个所有者的遗留资源实现类。
    unique_class = "lab.resources.legacy:UniqueContainer"
    # ``source_aliases`` 应保留唯一且无歧义的旧工作流源码兼容入口。
    source_aliases = compile_resource_template_source_aliases(
        [_resource_definition("legacy_plate", unique_class)]
    )

    assert source_aliases == {unique_class: "legacy_plate"}
