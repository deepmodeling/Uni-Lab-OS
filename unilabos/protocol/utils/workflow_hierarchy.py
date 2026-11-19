"""循环容器的层级关系：把跨越容器边界的依赖提升到同级。

工作流图里的 ``parent_uuid`` 有两种用途：编辑器分组（``group``，只是画布边界）和循环
容器（``loop``，真正的执行层级）。执行层级只看 loop：一个节点的**执行父级**是它祖先链
上最近的启用 loop 节点，找不到就是顶层。

DAG 按层级分别成环检测与调度：跨层的边 ``u -> v`` 提升为两者在最近公共容器下的祖先
``u' -> v'``——外部节点连到循环体里的节点，等价于外部节点先于整个循环；循环体里的节点
连到外部节点，等价于整个循环先于外部节点。容器与自己后代之间的边没有意义，拒绝。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

LOOP_KIND = "loop"


class HierarchyError(ValueError):
    """容器层级不合法（父链成环 / 容器与后代连边）。"""


def execution_parents(
    nodes: Mapping[str, Any],
    kinds: Mapping[str, str],
    *,
    enabled: Optional[Iterable[str]] = None,
    parent_of: Any = None,
) -> Dict[str, Optional[str]]:
    """每个启用节点的执行父级：祖先链上最近的**启用** loop 节点。

    ``nodes`` 是完整图（含组框等不参与执行的节点，父链要经过它们），``enabled`` 是
    参与执行的节点集合（缺省 = 全部）。``parent_of(node)`` 取原始 ``parent_uuid``
    （dict 或 pydantic 节点都可）。非 loop 的中间父级（组框）被跳过——组框里的节点
    仍是顶层节点。
    """

    getter = parent_of or _default_parent_of
    raw_parent = {uuid: getter(node) for uuid, node in nodes.items()}
    active = set(nodes if enabled is None else enabled)
    result: Dict[str, Optional[str]] = {}
    for uuid in nodes:
        if uuid not in active:
            continue
        # 先走完整条父链做环检测，再取最近的启用 loop
        chain: List[str] = []
        current = raw_parent.get(uuid)
        while current is not None:
            if current == uuid or current in chain:
                raise HierarchyError("父子关系形成循环")
            chain.append(current)
            current = raw_parent.get(current)
        result[uuid] = next(
            (item for item in chain if item in active and kinds.get(item) == LOOP_KIND),
            None,
        )
    return result


def ancestors(parents: Mapping[str, Optional[str]], uuid: str) -> List[str]:
    """从直接父级到根的祖先链。"""

    chain: List[str] = []
    current = parents.get(uuid)
    while current is not None:
        if current in chain:
            raise HierarchyError("父子关系形成循环")
        chain.append(current)
        current = parents.get(current)
    return chain


def lift_edge(
    parents: Mapping[str, Optional[str]], source: str, target: str
) -> Tuple[str, str]:
    """把 ``source -> target`` 提升到最近公共容器下的同级节点对。

    返回值两端属于同一层级（同一个父容器或都在顶层）。一端是另一端的祖先时抛
    ``HierarchyError``。
    """

    source_chain = [source, *ancestors(parents, source)]
    target_chain = [target, *ancestors(parents, target)]
    if target in source_chain[1:] or source in target_chain[1:]:
        raise HierarchyError("循环节点不能与自己循环体内的节点连线")
    target_set = set(target_chain)
    common = next((item for item in source_chain[1:] if item in target_set), None)
    lifted_source = _child_below(source_chain, common)
    lifted_target = _child_below(target_chain, common)
    return lifted_source, lifted_target


def _child_below(chain: List[str], container: Optional[str]) -> str:
    if container is None:
        return chain[-1]
    return chain[chain.index(container) - 1]


def children_of(parents: Mapping[str, Optional[str]]) -> Dict[Optional[str], List[str]]:
    """容器 -> 直接子节点列表（``None`` 键是顶层）。保持 ``parents`` 的迭代序。"""

    grouped: Dict[Optional[str], List[str]] = {}
    for uuid, parent in parents.items():
        grouped.setdefault(parent, []).append(uuid)
    return grouped


def lifted_pairs(
    parents: Mapping[str, Optional[str]],
    pairs: Iterable[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    """批量提升依赖对，去重、去自环（同一容器内部的边提升后不变）。"""

    seen: set[Tuple[str, str]] = set()
    result: List[Tuple[str, str]] = []
    for source, target in pairs:
        lifted = lift_edge(parents, source, target)
        if lifted[0] == lifted[1] or lifted in seen:
            continue
        seen.add(lifted)
        result.append(lifted)
    return result


def _default_parent_of(node: Any) -> Optional[str]:
    if isinstance(node, Mapping):
        value = node.get("parent_uuid")
    else:
        value = getattr(node, "parent_uuid", None)
    return str(value) if value else None


__all__ = [
    "HierarchyError",
    "LOOP_KIND",
    "ancestors",
    "children_of",
    "execution_parents",
    "lift_edge",
    "lifted_pairs",
]
