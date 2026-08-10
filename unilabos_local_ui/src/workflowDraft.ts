import { stripAutomaticallyManagedActionParameters } from './automaticActionParameters';

type WorkflowDraftNode = {
  id: string;
  position: unknown;
  data: {
    deviceId?: string;
    device_id?: string;
    method: string;
    label: string;
    description: string;
    params: Record<string, unknown>;
    paramSpecs?: ParamSpecLike[];
    opcVariables?: string[];
    executionDisabled?: boolean;
    executionBypassed?: boolean;
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

type ParamSpecLike = {
  name?: string;
  label?: string;
  description?: string;
  type?: string;
  min?: number;
  max?: number;
  default?: unknown;
  unit?: string;
  options?: Array<{ value: string | number | boolean; label: string }>;
};

type ActionSpecLike = {
  method: string;
  label: string;
  description: string;
  device_id?: string;
  params?: ParamSpecLike[];
  opc_variables?: string[];
};

type ImportedDraftOptions = {
  autoLayout?: boolean;
};

type ExecutionReason = 'willRun' | 'beforeStart' | 'disabled' | 'blockedByDisabled' | 'disconnected' | 'bypassed';

const DEFAULT_START_X = 80;
const DEFAULT_START_Y = 120;
const LAYOUT_X_GAP = 240;
const LAYOUT_Y_GAP = 140;
const MAX_NODES_PER_ROW = 6;
const MAX_OPC_VARIABLES_PER_NODE = 500;

function normalizeOpcVariables(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > MAX_OPC_VARIABLES_PER_NODE) {
    throw new Error('workflow 节点 opc_variables 必须是最多 500 项的数组');
  }
  const variables: string[] = [];
  value.forEach((item) => {
    if (typeof item !== 'string' || !item.trim()) {
      throw new Error('workflow 节点 opc_variables 必须是非空字符串数组');
    }
    const variable = item.trim();
    if (!variables.includes(variable)) variables.push(variable);
  });
  return variables;
}

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
        opc_variables: normalizeOpcVariables(node.data.opcVariables || []),
      };
      if (node.data.deviceId) {
        data.device_id = node.data.deviceId;
      }
      if (node.data.executionDisabled) {
        data.execution_disabled = true;
      }
      if (node.data.executionBypassed) {
        data.execution_bypassed = true;
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

export function createExecutionPlan<T extends FlowNodeLike>(
  nodes: T[],
  edges: FlowEdgeLike[],
  startNodeId?: string | null,
) {
  const nodeIds = new Set(nodes.map((node) => node.id));
  const normalizedStartNodeId = startNodeId && nodeIds.has(startNodeId) ? startNodeId : null;
  const reachableFromStart = collectReachableNodeIds(normalizedStartNodeId, nodes, edges);
  const executionGraph = createBypassedExecutionGraph(nodes, edges);
  const disabledSeeds = new Set(
    executionGraph.nodes.filter((node) => node.data.executionDisabled).map((node) => node.id),
  );
  const reachableDisabledSeeds = new Set(Array.from(disabledSeeds).filter((nodeId) => reachableFromStart.has(nodeId)));
  const blockedByDisabled = new Set<string>();
  reachableDisabledSeeds.forEach((nodeId) => {
    collectReachableNodeIds(nodeId, executionGraph.nodes, executionGraph.edges).forEach(
      (blockedId) => blockedByDisabled.add(blockedId),
    );
  });

  const nodeStates: Record<string, { reason: ExecutionReason }> = {};
  nodes.forEach((node) => {
    let reason: ExecutionReason = 'willRun';
    if (node.data.executionBypassed) {
      reason = 'bypassed';
    } else if (!reachableFromStart.has(node.id)) {
      reason = 'beforeStart';
    } else if (reachableDisabledSeeds.has(node.id)) {
      reason = 'disabled';
    } else if (blockedByDisabled.has(node.id)) {
      reason = 'blockedByDisabled';
    }
    nodeStates[node.id] = { reason };
  });

  const executableNodeIdsForPlan = new Set(
    executionGraph.nodes.filter((node) => nodeStates[node.id]?.reason === 'willRun').map((node) => node.id),
  );
  const executableEdges = executionGraph.edges.filter(
    (edge) => executableNodeIdsForPlan.has(edge.source) && executableNodeIdsForPlan.has(edge.target),
  );
  const executableNodes = orderNodesByDag(
    executionGraph.nodes.filter((node) => executableNodeIdsForPlan.has(node.id)),
    executableEdges,
  );
  const disabledNodeId = nodes.find((node) => reachableDisabledSeeds.has(node.id))?.id || null;

  return {
    startNodeId: normalizedStartNodeId,
    disabledNodeId,
    executableNodes,
    executableEdges,
    nodeStates,
    totalCount: nodes.length,
    executableCount: executableNodes.length,
  };
}

export function createExecutionEdgeOverlay(
  originalEdges: FlowEdgeLike[],
  executableEdges: FlowEdgeLike[],
) {
  const originalEndpoints = new Set(
    originalEdges.map((edge) => JSON.stringify([edge.source, edge.target])),
  );
  const overlayEndpoints = new Set<string>();
  const reservedIds = new Set(originalEdges.map((edge) => edge.id));

  return executableEdges.flatMap((edge) => {
    const endpointKey = JSON.stringify([edge.source, edge.target]);
    if (originalEndpoints.has(endpointKey) || overlayEndpoints.has(endpointKey)) {
      return [];
    }
    overlayEndpoints.add(endpointKey);

    const baseId = createExecutionOverlayEdgeId(edge.source, edge.target);
    let id = baseId;
    let collisionIndex = 1;
    while (reservedIds.has(id)) {
      id = `${baseId}#${collisionIndex}`;
      collisionIndex += 1;
    }
    reservedIds.add(id);
    return [{ id, source: edge.source, target: edge.target }];
  });
}

export function createImportedDraft(
  payload: unknown,
  actions: ActionSpecLike[],
  options: ImportedDraftOptions = {},
) {
  const actionByMethod = new Map(actions.map((action) => [action.method, action]));
  const imported = normalizeImportedPayload(payload, actionByMethod);
  const nodes = (options.autoLayout ?? true) ? layoutFlowGraph(imported.nodes, imported.edges) : imported.nodes;
  return { ...imported, nodes };
}

export function layoutFlowGraph<T extends { id: string; position: unknown }>(
  nodes: T[],
  edges: FlowEdgeLike[],
): T[] {
  if (!nodes.length) return nodes;
  if (!edges.length) return applyGridLayout(nodes);

  const outgoing = new Map(nodes.map((node) => [node.id, [] as string[]]));
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  edges.forEach((edge) => {
    if (!nodesById.has(edge.source) || !nodesById.has(edge.target)) return;
    outgoing.get(edge.source)?.push(edge.target);
  });

  const columns = new Map(nodes.map((node) => [node.id, 0]));
  const ordered = orderNodeIdsByDag(nodes, edges, (current, target) => {
    columns.set(target, Math.max(columns.get(target) || 0, (columns.get(current) || 0) + 1));
  });

  if (ordered.length !== nodes.length) {
    return applyGridLayout(nodes);
  }

  const branchAnchor = ordered.find((nodeId) => (outgoing.get(nodeId) || []).length > 1);
  if (branchAnchor) {
    return applyBranchedLayout(nodes, ordered, outgoing, branchAnchor);
  }

  const rowsByColumn = new Map<number, number>();
  return ordered.map((nodeId) => {
    const node = nodesById.get(nodeId)!;
    const column = columns.get(nodeId) || 0;
    const wrappedColumn = column % MAX_NODES_PER_ROW;
    const wrappedRow = Math.floor(column / MAX_NODES_PER_ROW);
    const stackRow = rowsByColumn.get(column) || 0;
    rowsByColumn.set(column, stackRow + 1);
    return {
      ...node,
      position: {
        x: DEFAULT_START_X + wrappedColumn * LAYOUT_X_GAP,
        y: DEFAULT_START_Y + (wrappedRow + stackRow) * LAYOUT_Y_GAP,
      },
    };
  });
}

const ESTIMATED_NODE_WIDTH = 220;

export function expandLayoutToWidth<T extends { position: { x: number; y: number } }>(
  nodes: T[],
  targetWidth: number,
  sidePadding = 72,
): T[] {
  if (!nodes.length || targetWidth <= sidePadding * 2) return nodes;

  const xs = nodes.map((node) => Number(node.position.x) || DEFAULT_START_X);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const contentWidth = maxX - minX + ESTIMATED_NODE_WIDTH;
  const availableWidth = targetWidth - sidePadding * 2;

  if (contentWidth >= availableWidth) return nodes;

  const scale = availableWidth / contentWidth;
  return nodes.map((node) => ({
    ...node,
    position: {
      x: sidePadding + (((Number(node.position.x) || DEFAULT_START_X) - minX) * scale),
      y: Number(node.position.y) || DEFAULT_START_Y,
    },
  }));
}

function applyBranchedLayout<T extends { id: string; position: unknown }>(
  nodes: T[],
  ordered: string[],
  outgoing: Map<string, string[]>,
  branchAnchor: string,
): T[] {
  const originalIndex = new Map(nodes.map((node, index) => [node.id, index]));
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const positions = new Map<string, { x: number; y: number }>();
  const anchorOrderIndex = ordered.indexOf(branchAnchor);
  const nodesBeforeAnchor = ordered.slice(0, Math.max(anchorOrderIndex, 0));
  const branchChildren = [...(outgoing.get(branchAnchor) || [])].sort(
    (left, right) => (originalIndex.get(left) || 0) - (originalIndex.get(right) || 0),
  );
  const joinNode = findFirstCommonJoin(branchChildren, ordered, outgoing);

  nodesBeforeAnchor.forEach((nodeId, index) => {
    const wrappedColumn = index % MAX_NODES_PER_ROW;
    const wrappedRow = Math.floor(index / MAX_NODES_PER_ROW);
    positions.set(nodeId, {
      x: DEFAULT_START_X + wrappedColumn * LAYOUT_X_GAP,
      y: DEFAULT_START_Y + wrappedRow * LAYOUT_Y_GAP,
    });
  });

  const previousRow = nodesBeforeAnchor.length
    ? Math.floor((nodesBeforeAnchor.length - 1) / MAX_NODES_PER_ROW)
    : -1;
  const anchorRow = previousRow + 1;
  positions.set(branchAnchor, positionAt(0, anchorRow));

  const branchPaths = branchChildren.map((childId) => collectBranchPath(childId, joinNode, outgoing));
  branchPaths.forEach((path, branchIndex) => {
    const row = anchorRow + 1 + branchIndex;
    path.forEach((nodeId, stepIndex) => {
      positions.set(nodeId, positionAt(stepIndex + 1, row));
    });
  });

  const longestBranchLength = branchPaths.reduce((maxLength, path) => Math.max(maxLength, path.length), 0);
  let continuationColumn = Math.max(1, longestBranchLength + 1);
  if (joinNode) {
    positions.set(joinNode, positionAt(continuationColumn, anchorRow));
    continuationColumn += 1;
  }

  ordered.forEach((nodeId) => {
    if (positions.has(nodeId) || branchPaths.some((path) => path.includes(nodeId))) return;
    if (anchorOrderIndex >= 0 && ordered.indexOf(nodeId) < anchorOrderIndex) return;
    positions.set(nodeId, positionAt(continuationColumn, anchorRow));
    continuationColumn += 1;
  });

  return ordered.map((nodeId, index) => {
    const node = nodesById.get(nodeId)!;
    return {
      ...node,
      position: positions.get(node.id) || positionAt(index % MAX_NODES_PER_ROW, Math.floor(index / MAX_NODES_PER_ROW)),
    };
  });
}

function orderNodesByDag<T extends { id: string }>(nodes: T[], edges: FlowEdgeLike[]): T[] {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const orderedIds = orderNodeIdsByDag(nodes, edges);
  if (orderedIds.length !== nodes.length) return nodes;
  return orderedIds.flatMap((nodeId) => {
    const node = nodesById.get(nodeId);
    return node ? [node] : [];
  });
}

function orderNodeIdsByDag<T extends { id: string }>(
  nodes: T[],
  edges: FlowEdgeLike[],
  onEdgeVisited?: (source: string, target: string) => void,
) {
  const ids = new Set(nodes.map((node) => node.id));
  const originalIndex = new Map(nodes.map((node, index) => [node.id, index]));
  const incoming = new Map(nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(nodes.map((node) => [node.id, [] as string[]]));

  edges.forEach((edge) => {
    if (!ids.has(edge.source) || !ids.has(edge.target)) return;
    outgoing.get(edge.source)?.push(edge.target);
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1);
  });

  const ready = nodes.filter((node) => (incoming.get(node.id) || 0) === 0).map((node) => node.id);
  const ordered: string[] = [];

  while (ready.length) {
    ready.sort((left, right) => (originalIndex.get(left) || 0) - (originalIndex.get(right) || 0));
    const current = ready.shift()!;
    ordered.push(current);
    for (const target of outgoing.get(current) || []) {
      onEdgeVisited?.(current, target);
      incoming.set(target, (incoming.get(target) || 0) - 1);
      if ((incoming.get(target) || 0) === 0) ready.push(target);
    }
  }

  return ordered;
}

function findFirstCommonJoin(
  branchChildren: string[],
  ordered: string[],
  outgoing: Map<string, string[]>,
) {
  if (branchChildren.length < 2) return null;

  const distanceByBranch = branchChildren.map((childId) => collectReachableDistances(childId, outgoing));
  const commonCandidates = ordered.filter((nodeId) =>
    !branchChildren.includes(nodeId) && distanceByBranch.every((distances) => distances.has(nodeId)),
  );
  if (!commonCandidates.length) return null;

  return commonCandidates.sort((left, right) => {
    const leftDistance = Math.max(...distanceByBranch.map((distances) => distances.get(left) || 0));
    const rightDistance = Math.max(...distanceByBranch.map((distances) => distances.get(right) || 0));
    if (leftDistance !== rightDistance) return leftDistance - rightDistance;
    return ordered.indexOf(left) - ordered.indexOf(right);
  })[0];
}

function collectReachableDistances(startNodeId: string, outgoing: Map<string, string[]>) {
  const distances = new Map<string, number>();
  const pending: Array<{ nodeId: string; distance: number }> = [{ nodeId: startNodeId, distance: 0 }];

  while (pending.length) {
    const { nodeId, distance } = pending.shift()!;
    const currentDistance = distances.get(nodeId);
    if (currentDistance !== undefined && currentDistance <= distance) continue;
    distances.set(nodeId, distance);
    for (const target of outgoing.get(nodeId) || []) {
      pending.push({ nodeId: target, distance: distance + 1 });
    }
  }

  return distances;
}

function collectBranchPath(startNodeId: string, joinNode: string | null, outgoing: Map<string, string[]>) {
  const path: string[] = [];
  let current: string | undefined = startNodeId;
  const seen = new Set<string>();

  while (current && current !== joinNode && !seen.has(current)) {
    path.push(current);
    seen.add(current);
    const nextNodes: string[] = outgoing.get(current) || [];
    if (nextNodes.length !== 1) break;
    current = nextNodes[0];
  }

  return path;
}

function positionAt(column: number, row: number) {
  return {
    x: DEFAULT_START_X + column * LAYOUT_X_GAP,
    y: DEFAULT_START_Y + row * LAYOUT_Y_GAP,
  };
}

function normalizeImportedPayload(payload: unknown, actionByMethod: Map<string, ActionSpecLike>) {
  const data = asRecord(payload, '导入文件必须是 JSON 对象');
  if (Array.isArray(data.rules)) {
    return normalizePseudoFlowPayload(data, actionByMethod);
  }
  if (Array.isArray(data.nodes) && Array.isArray(data.edges)) {
    return normalizeCanvasDraftPayload(data, actionByMethod);
  }
  throw new Error('不支持的 Flow JSON 格式，请导入 UI 导出的 Flow JSON 或画布草稿 JSON');
}

function normalizePseudoFlowPayload(data: Record<string, unknown>, actionByMethod: Map<string, ActionSpecLike>) {
  const rules = data.rules as unknown[];
  const firstRule = asRecord(rules[0], 'Flow JSON 缺少 rules[0]');
  const actionItems = Array.isArray(firstRule.actions) ? firstRule.actions : [];
  if (!actionItems.length) {
    throw new Error('Flow JSON 中没有可导入的动作');
  }

  const nodes = actionItems.map((item, index) => {
    const action = asRecord(asRecord(item, 'Flow JSON 动作格式错误').action, 'Flow JSON 动作格式错误');
    const method = readRequiredString(action.method, 'Flow JSON 动作缺少 method');
    const id = readOptionalString(action.workflow_node_id) || `node_${index + 1}_${method}`;
    const executionFlags = normalizeExecutionFlags(action);
    return buildFlowNode(
      id,
      method,
      action.params,
      actionByMethod,
      executionFlags.executionDisabled,
      executionFlags.executionBypassed,
    );
  });
  const edges = nodes.slice(1).map((node, index) => ({
    id: `${nodes[index].id}-${node.id}`,
    source: nodes[index].id,
    target: node.id,
  }));
  return {
    name: readOptionalString(data.name) || readOptionalString(firstRule.name) || 'imported_flow',
    nodes,
    edges,
  };
}

function normalizeCanvasDraftPayload(data: Record<string, unknown>, actionByMethod: Map<string, ActionSpecLike>) {
  const nodes = (data.nodes as unknown[]).map((item, index) => {
    const node = asRecord(item, '画布草稿节点格式错误');
    const nodeData = asRecord(node.data, '画布草稿节点缺少 data');
    const method = readRequiredString(nodeData.method, '画布草稿节点缺少 method');
    const id = readOptionalString(node.id) || `node_${index + 1}_${method}`;
    const executionFlags = normalizeExecutionFlags(nodeData);
    return {
      ...buildFlowNode(
        id,
        method,
        nodeData.params,
        actionByMethod,
        executionFlags.executionDisabled,
        executionFlags.executionBypassed,
      ),
      position: normalizePosition(node.position),
    };
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = (data.edges as unknown[]).flatMap((item, index) => {
    const edge = asRecord(item, '画布草稿连线格式错误');
    const source = readOptionalString(edge.source);
    const target = readOptionalString(edge.target);
    if (!source || !target || !nodeIds.has(source) || !nodeIds.has(target)) {
      return [];
    }
    return [{
      id: readOptionalString(edge.id) || `${source}-${target}-${index}`,
      source,
      target,
    }];
  });
  return {
    name: readOptionalString(data.name) || 'imported_flow',
    nodes,
    edges,
  };
}

function buildFlowNode(
  id: string,
  method: string,
  importedParams: unknown,
  actionByMethod: Map<string, ActionSpecLike>,
  executionDisabled = false,
  executionBypassed = false,
) {
  const action = actionByMethod.get(method);
  if (!action) {
    throw new Error(`导入失败：当前 preset 不包含动作 ${method}`);
  }
  const params = stripAutomaticallyManagedActionParameters(method, {
    ...buildDefaultParams(action.params || []),
    ...normalizeParams(importedParams),
  }, id);
  return {
    id,
    type: 'actionNode',
    position: { x: DEFAULT_START_X, y: DEFAULT_START_Y },
    data: {
      deviceId: action.device_id,
      method,
      label: action.label,
      description: action.description,
      params,
      paramSpecs: action.params || [],
      opcVariables: action.opc_variables || [],
      runStatus: 'idle',
      executionDisabled: executionBypassed ? false : executionDisabled,
      executionBypassed,
    },
  };
}

function normalizeExecutionFlags(data: Record<string, unknown>) {
  const executionBypassed = Boolean(data.execution_bypassed || data.executionBypassed);
  return {
    executionBypassed,
    executionDisabled: executionBypassed
      ? false
      : Boolean(data.execution_disabled || data.executionDisabled),
  };
}

function createBypassedExecutionGraph<T extends FlowNodeLike>(nodes: T[], edges: FlowEdgeLike[]) {
  const bypassedNodeIds = new Set(
    nodes.filter((node) => node.data.executionBypassed).map((node) => node.id),
  );
  if (!bypassedNodeIds.size) {
    return { nodes, edges };
  }

  let workingEdges = [...edges];
  nodes.forEach((node) => {
    if (!bypassedNodeIds.has(node.id)) return;
    const incoming = workingEdges.filter((edge) => edge.target === node.id && edge.source !== node.id);
    const outgoing = workingEdges.filter((edge) => edge.source === node.id && edge.target !== node.id);
    workingEdges = workingEdges.filter((edge) => edge.source !== node.id && edge.target !== node.id);
    incoming.forEach((incomingEdge) => {
      outgoing.forEach((outgoingEdge) => {
        if (incomingEdge.source === outgoingEdge.target) return;
        workingEdges.push({
          id: createBypassEdgeId(incomingEdge.source, outgoingEdge.target),
          source: incomingEdge.source,
          target: outgoingEdge.target,
        });
      });
    });
    workingEdges = deduplicateEdges(workingEdges);
  });

  const executionNodes = nodes.filter((node) => !bypassedNodeIds.has(node.id));
  const executionNodeIds = new Set(executionNodes.map((node) => node.id));
  return {
    nodes: executionNodes,
    edges: ensureUniqueEdgeIds(
      deduplicateEdges(
        workingEdges.filter(
          (edge) =>
            edge.source !== edge.target &&
            executionNodeIds.has(edge.source) &&
            executionNodeIds.has(edge.target),
        ),
      ),
    ),
  };
}

function deduplicateEdges(edges: FlowEdgeLike[]) {
  const seen = new Set<string>();
  return edges.filter((edge) => {
    const key = JSON.stringify([edge.source, edge.target]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function ensureUniqueEdgeIds(edges: FlowEdgeLike[]) {
  const reservedIds = new Set(edges.map((edge) => edge.id));
  const assignedIds = new Set<string>();
  const resolvedIds = new Map<FlowEdgeLike, string>();
  const sortedEdges = [...edges].sort((left, right) => {
    const leftKey = JSON.stringify([left.id, left.source, left.target]);
    const rightKey = JSON.stringify([right.id, right.source, right.target]);
    return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
  });

  sortedEdges.forEach((edge) => {
    let id = edge.id;
    if (assignedIds.has(id)) {
      const suffix = `${edge.source.length}:${edge.source}:${edge.target.length}:${edge.target}`;
      id = `${edge.id}#${suffix}`;
      let collisionIndex = 1;
      while (reservedIds.has(id) || assignedIds.has(id)) {
        id = `${edge.id}#${suffix}:${collisionIndex}`;
        collisionIndex += 1;
      }
    }
    assignedIds.add(id);
    resolvedIds.set(edge, id);
  });

  return edges.map((edge) => {
    const id = resolvedIds.get(edge)!;
    return id === edge.id ? edge : { ...edge, id };
  });
}

function createBypassEdgeId(source: string, target: string) {
  return `bypass:${source.length}:${source}:${target.length}:${target}`;
}

function createExecutionOverlayEdgeId(source: string, target: string) {
  return `execution-derived:${source.length}:${source}:${target.length}:${target}`;
}

function collectReachableNodeIds(
  startNodeId: string | null,
  nodes: Array<{ id: string }>,
  edges: FlowEdgeLike[],
) {
  const nodeIds = new Set(nodes.map((node) => node.id));
  if (!startNodeId || !nodeIds.has(startNodeId)) {
    return nodeIds;
  }
  const outgoing = new Map(nodes.map((node) => [node.id, [] as string[]]));
  edges.forEach((edge) => {
    if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) {
      outgoing.get(edge.source)?.push(edge.target);
    }
  });
  const reachable = new Set<string>();
  const pending = [startNodeId];
  while (pending.length) {
    const current = pending.shift()!;
    if (reachable.has(current)) continue;
    reachable.add(current);
    pending.push(...(outgoing.get(current) || []));
  }
  return reachable;
}

function buildDefaultParams(params: ParamSpecLike[]) {
  return params.reduce<Record<string, unknown>>((defaults, param) => {
    const name = param.name || '';
    if (!name) return defaults;
    if ('default' in param) {
      defaults[name] = param.default;
    } else if (param.type === 'boolean') {
      defaults[name] = false;
    } else if (param.type === 'integer' || param.type === 'number') {
      defaults[name] = param.min ?? 0;
    } else {
      defaults[name] = '';
    }
    return defaults;
  }, {});
}

function applyGridLayout<T extends { position: unknown }>(nodes: T[]): T[] {
  return nodes.map((node, index) => ({
    ...node,
    position: {
      x: DEFAULT_START_X + (index % MAX_NODES_PER_ROW) * LAYOUT_X_GAP,
      y: DEFAULT_START_Y + Math.floor(index / MAX_NODES_PER_ROW) * LAYOUT_Y_GAP,
    },
  }));
}

function normalizePosition(position: unknown) {
  const value = asOptionalRecord(position);
  const x = typeof value?.x === 'number' ? value.x : DEFAULT_START_X;
  const y = typeof value?.y === 'number' ? value.y : DEFAULT_START_Y;
  return { x, y };
}

function normalizeParams(params: unknown) {
  const value = asOptionalRecord(params);
  return value ? { ...value } : {};
}

function asRecord(value: unknown, message: string): Record<string, unknown> {
  const record = asOptionalRecord(value);
  if (!record) throw new Error(message);
  return record;
}

function asOptionalRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function readRequiredString(value: unknown, message: string) {
  const text = readOptionalString(value);
  if (!text) throw new Error(message);
  return text;
}

function readOptionalString(value: unknown) {
  return typeof value === 'string' && value.trim() ? value.trim() : '';
}
