type WorkflowDraftNode = {
  id: string;
  position: unknown;
  data: {
    deviceId?: string;
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
    nodes: nodes.map((node) => {
      const data: Record<string, unknown> = {
        method: node.data.method,
        label: node.data.label,
        description: node.data.description,
        params: node.data.params,
      };
      if (node.data.deviceId) {
        data.device_id = node.data.deviceId;
      }
      return {
        id: node.id,
        position: node.position,
        data,
      };
    }),
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
