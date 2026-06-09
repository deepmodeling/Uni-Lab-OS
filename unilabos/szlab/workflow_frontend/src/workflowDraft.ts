type WorkflowDraftNode = {
  id: string;
  position: unknown;
  data: {
    method: string;
    label: string;
    description: string;
    params: Record<string, unknown>;
  };
};

type WorkflowDraftEdge = {
  id: string;
  source: string;
  target: string;
};

type FlowNodeLike = WorkflowDraftNode & {
  data: WorkflowDraftNode['data'] & {
    runStatus?: unknown;
    onPositionChange?: unknown;
  };
};

type FlowEdgeLike = WorkflowDraftEdge;

export function createWorkflowRequest(
  name: string,
  nodes: FlowNodeLike[],
  edges: FlowEdgeLike[],
) {
  return {
    name,
    nodes: nodes.map((node) => ({
      id: node.id,
      position: node.position,
      data: {
        method: node.data.method,
        label: node.data.label,
        description: node.data.description,
        params: node.data.params,
      },
    })),
    edges: edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
    })),
  };
}

export function workflowDraftKey(name: string, nodes: FlowNodeLike[], edges: FlowEdgeLike[]) {
  return JSON.stringify(createWorkflowRequest(name, nodes, edges));
}
