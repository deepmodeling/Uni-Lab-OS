"""Internal DAG models for the scheduler core."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StepNode:
    """A single step in a task DAG."""

    step_id: str
    task_id: str
    machine_type: str
    duration: float
    priority_weight: float = 1.0
    input_samples: list[str] = field(default_factory=list)
    output_samples: list[str] = field(default_factory=list)

    # 运行时状态
    ready: bool = False
    completed: bool = False
    start: float | None = None
    end: float | None = None
    assigned_device: str | None = None

    def reset(self) -> None:
        self.ready = False
        self.completed = False
        self.start = None
        self.end = None
        self.assigned_device = None


@dataclass
class Edge:
    """Dependency edge: source → target."""

    source: str  # step_id
    target: str  # step_id


@dataclass
class TimeConstraint:
    """步骤间时间窗约束."""

    from_step: str
    to_step: str
    min_gap: int | None = None  # start[to] - end[from] ≥ min_gap
    max_gap: int | None = None  # start[to] - end[from] ≤ max_gap


@dataclass
class TaskDAG:
    """A single task containing its step nodes and dependency edges."""

    task_id: str
    priority: float
    weight: float = 1.0
    nodes: dict[str, StepNode] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    time_constraints: list[TimeConstraint] = field(default_factory=list)
    parents: dict[str, list[str]] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)

    def build_adjacency(self) -> None:
        """从 edges 构建 parents/children 邻接映射."""
        self.parents = {nid: [] for nid in self.nodes}
        self.children = {nid: [] for nid in self.nodes}
        for edge in self.edges:
            self.children[edge.source].append(edge.target)
            self.parents[edge.target].append(edge.source)
