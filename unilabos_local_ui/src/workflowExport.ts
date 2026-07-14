type WorkflowExportNode = {
  id: string;
  data: {
    label: string;
    method: string;
    deviceId?: string;
    params: Record<string, unknown>;
    executionBypassed?: boolean;
  };
};

type WorkflowExportEdge = {
  source: string;
  target: string;
};

export type PseudoFlowJson = {
  name: string;
  rules: Array<Record<string, unknown>>;
};

export function createPseudoFlowJson(
  name: string,
  nodes: WorkflowExportNode[],
  edges: WorkflowExportEdge[],
): PseudoFlowJson {
  const orderedNodes = orderFlowNodes(nodes, edges);
  const flowName = name || 'pseudo_flow';
  return {
    name: flowName,
    rules: [
      {
        name: flowName,
        trigger: {
          node: orderedNodes[0]?.data.label || flowName,
          value: true,
          edge: 'rising',
        },
        log_nodes: orderedNodes.map((node) => node.data.label),
        actions: orderedNodes.map((node, index) => {
          const action: Record<string, unknown> = {
            index: index + 1,
            node: node.data.label,
            workflow_node_id: node.id,
            device_id: node.data.deviceId,
            method: node.data.method,
            params: node.data.params,
          };
          if (node.data.executionBypassed) {
            action.execution_bypassed = true;
          }
          return { action };
        }),
      },
    ],
  };
}

function orderFlowNodes(nodes: WorkflowExportNode[], edges: WorkflowExportEdge[]) {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const originalIndex = new Map(nodes.map((node, index) => [node.id, index]));
  const incoming = new Map(nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(nodes.map((node) => [node.id, [] as string[]]));

  edges.forEach((edge) => {
    if (!nodesById.has(edge.source) || !nodesById.has(edge.target)) return;
    outgoing.get(edge.source)?.push(edge.target);
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1);
  });

  const ready = nodes
    .filter((node) => (incoming.get(node.id) || 0) === 0)
    .map((node) => node.id);
  const orderedIds: string[] = [];

  while (ready.length) {
    ready.sort((left, right) => (originalIndex.get(left) || 0) - (originalIndex.get(right) || 0));
    const current = ready.shift()!;
    orderedIds.push(current);
    (outgoing.get(current) || []).forEach((target) => {
      incoming.set(target, (incoming.get(target) || 0) - 1);
      if ((incoming.get(target) || 0) === 0) ready.push(target);
    });
  }

  return orderedIds.map((id) => nodesById.get(id)).filter((node): node is WorkflowExportNode => Boolean(node));
}
