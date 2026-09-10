"""dag_state 对齐 Go canRunNodes / clearFinishedNode / detectCycle 行为测试。"""

import pytest

from unilabos.app.scheduler.dag_state import WorkflowCycleError, WorkflowRun
from unilabos.app.scheduler.models import (
    Handle,
    NodeState,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
)


def _node(node_id: str, device: str = "dev1", action: str = "act") -> WorkflowNode:
    return WorkflowNode(id=node_id, device_id=device, action_name=action, action_type="goal")


def _edge(src: str, dst: str, sh: str = "", th: str = "") -> WorkflowEdge:
    return WorkflowEdge(
        uuid=f"{src}->{dst}",
        source_node_id=src,
        target_node_id=dst,
        source_handle_uuid=sh,
        target_handle_uuid=th,
    )


def _diamond_spec() -> WorkflowSpec:
    r"""A → B, A → C, B → D, C → D."""
    return WorkflowSpec(
        workflow_id="wf1",
        nodes=[_node("A"), _node("B"), _node("C"), _node("D")],
        edges=[_edge("A", "B"), _edge("A", "C"), _edge("B", "D"), _edge("C", "D")],
    )


class TestBuild:
    def test_ready_is_indegree_zero(self):
        run = WorkflowRun(_diamond_spec())
        assert [n.id for n in run.ready_nodes()] == ["A"]

    def test_cycle_detection(self):
        # Go detectCycle: A→B→C→A 抛 WorkflowHasCircularErr
        spec = WorkflowSpec(
            workflow_id="wf-cycle",
            nodes=[_node("A"), _node("B"), _node("C")],
            edges=[_edge("A", "B"), _edge("B", "C"), _edge("C", "A")],
        )
        with pytest.raises(WorkflowCycleError):
            WorkflowRun(spec)

    def test_edge_to_unknown_node_ignored(self):
        # Go loadData: 过滤 source/target 不存在的边
        spec = WorkflowSpec(
            workflow_id="wf2",
            nodes=[_node("A"), _node("B")],
            edges=[_edge("A", "B"), _edge("ghost", "B"), _edge("A", "ghost")],
        )
        run = WorkflowRun(spec)
        assert [n.id for n in run.ready_nodes()] == ["A"]

    def test_disabled_node_excluded(self):
        spec = WorkflowSpec(
            workflow_id="wf3",
            nodes=[_node("A"), WorkflowNode(id="B", disabled=True)],
            edges=[],
        )
        run = WorkflowRun(spec)
        assert [n.id for n in run.ready_nodes()] == ["A"]


class TestAdvance:
    def test_finish_advances_dependents(self):
        run = WorkflowRun(_diamond_spec())
        run.mark_dispatched("A")
        assert run.ready_nodes() == []  # A 已消费，B/C 仍有依赖

        run.mark_finished("A", {"ok": True})
        assert sorted(n.id for n in run.ready_nodes()) == ["B", "C"]

        run.mark_dispatched("B")
        run.mark_dispatched("C")
        run.mark_finished("B")
        # D 仍等 C
        assert run.ready_nodes() == []
        run.mark_finished("C")
        assert [n.id for n in run.ready_nodes()] == ["D"]

    def test_ready_not_consumed_until_dispatch(self):
        # canRunNodes 语义差异说明：Go 是取即消费；Edge 版拆成 ready→mark_dispatched
        # 两步，未下发（如设备忙跳过）的节点下次重排仍在 ready 集合。
        run = WorkflowRun(_diamond_spec())
        assert [n.id for n in run.ready_nodes()] == ["A"]
        assert [n.id for n in run.ready_nodes()] == ["A"]  # 幂等

    def test_workflow_succeeds_when_all_done(self):
        run = WorkflowRun(_diamond_spec())
        for node_id in ["A", "B", "C", "D"]:
            run.mark_dispatched(node_id)
            run.mark_finished(node_id)
        assert run.state is WorkflowState.SUCCESS

    def test_failed_node_fails_workflow(self):
        run = WorkflowRun(_diamond_spec())
        run.mark_dispatched("A")
        run.mark_failed("A")
        assert run.state is WorkflowState.FAILED
        assert run.ready_nodes() == []
        assert run.node_state("A") is NodeState.FAILED


class TestHandlePairFiltering:
    """Go buildNodeHandlePair 的过滤规则。"""

    def _spec_with_handles(self, source_handle: Handle, target_handle: Handle) -> WorkflowSpec:
        return WorkflowSpec(
            workflow_id="wf-h",
            nodes=[_node("A"), _node("B")],
            edges=[_edge("A", "B", sh=source_handle.uuid, th=target_handle.uuid)],
            handles=[source_handle, target_handle],
        )

    def test_executor_pair_collected(self):
        spec = self._spec_with_handles(
            Handle(uuid="s", data_source="executor", handle_key="out", data_key="v"),
            Handle(uuid="t", data_source="handle", handle_key="in", data_key="p"),
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 5})
        assert run.resolve_params("B") == {"p": 5}

    def test_non_executor_source_skipped(self):
        spec = self._spec_with_handles(
            Handle(uuid="s", data_source="static", handle_key="out", data_key="v"),
            Handle(uuid="t", data_source="handle", handle_key="in", data_key="p"),
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 5})
        assert run.resolve_params("B") == {}  # 没有传参对，原样返回

    def test_ready_handle_skipped(self):
        # handle_key == "ready" 只表达顺序依赖，不传参
        spec = self._spec_with_handles(
            Handle(uuid="s", data_source="executor", handle_key="ready", data_key="v"),
            Handle(uuid="t", data_source="handle", handle_key="in", data_key="p"),
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 5})
        assert run.resolve_params("B") == {}

    def test_empty_data_key_skipped(self):
        spec = self._spec_with_handles(
            Handle(uuid="s", data_source="executor", handle_key="out", data_key=""),
            Handle(uuid="t", data_source="handle", handle_key="in", data_key="p"),
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 5})
        assert run.resolve_params("B") == {}


class TestHandleKeyResolution:
    """新协议：workflow_edge 用 handle_key（模板内唯一）引用连接点。

    三级寻址：uuid（旧图）→ (node_id, handle_key)（新图）→ 全局唯一 key 兜底。
    """

    def test_key_scoped_by_node_id(self):
        # A、B 各有一个同名 handle_key（result/input 同名冲突场景），
        # node_id 限定后仍能取到正确的传参对。
        spec = WorkflowSpec(
            workflow_id="wf-key",
            nodes=[_node("A"), _node("B")],
            edges=[
                WorkflowEdge(
                    uuid="e1", source_node_id="A", target_node_id="B",
                    source_handle_key="result", target_handle_key="input",
                )
            ],
            handles=[
                Handle(handle_key="result", node_id="A", io_type="source",
                       data_source="executor", data_key="v"),
                Handle(handle_key="input", node_id="B", io_type="target",
                       data_source="handle", data_key="p"),
                # 干扰项：B 也有一个名为 result 的 handle（不同节点同名 key）
                Handle(handle_key="result", node_id="B", io_type="source",
                       data_source="executor", data_key="other"),
            ],
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 7})
        assert run.resolve_params("B") == {"p": 7}

    def test_global_unique_key_fallback_without_node_id(self):
        # payload 未带 node_id 时，key 全局唯一即可解析
        spec = WorkflowSpec(
            workflow_id="wf-key2",
            nodes=[_node("A"), _node("B")],
            edges=[
                WorkflowEdge(
                    uuid="e1", source_node_id="A", target_node_id="B",
                    source_handle_key="out", target_handle_key="in",
                )
            ],
            handles=[
                Handle(handle_key="out", data_source="executor", data_key="v"),
                Handle(handle_key="in", data_source="handle", data_key="p"),
            ],
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 3})
        assert run.resolve_params("B") == {"p": 3}

    def test_ambiguous_key_without_node_id_not_resolved(self):
        # 同名 key 无 node_id 限定 → 歧义不解析（依赖边仍生效，只是不传参）
        spec = WorkflowSpec(
            workflow_id="wf-key3",
            nodes=[_node("A"), _node("B")],
            edges=[
                WorkflowEdge(
                    uuid="e1", source_node_id="A", target_node_id="B",
                    source_handle_key="dup", target_handle_key="in",
                )
            ],
            handles=[
                Handle(handle_key="dup", data_source="executor", data_key="v"),
                Handle(handle_key="dup", data_source="executor", data_key="v2"),
                Handle(handle_key="in", data_source="handle", data_key="p"),
            ],
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 1})
        assert run.resolve_params("B") == {}

    def test_uuid_takes_precedence_over_key(self):
        # 新旧字段并存时优先 uuid（旧图行为完全不变）
        spec = WorkflowSpec(
            workflow_id="wf-key4",
            nodes=[_node("A"), _node("B")],
            edges=[
                WorkflowEdge(
                    uuid="e1", source_node_id="A", target_node_id="B",
                    source_handle_uuid="su", target_handle_uuid="tu",
                    source_handle_key="wrong", target_handle_key="wrong",
                )
            ],
            handles=[
                Handle(uuid="su", handle_key="out", data_source="executor", data_key="v"),
                Handle(uuid="tu", handle_key="in", data_source="handle", data_key="p"),
            ],
        )
        run = WorkflowRun(spec)
        run.mark_finished("A", {"v": 9})
        assert run.resolve_params("B") == {"p": 9}
