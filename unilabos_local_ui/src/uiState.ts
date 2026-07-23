type WorkflowNodeLike = {
  data?: {
    deviceId?: string;
    runStatus?: string;
  };
};

type WorkspaceSummaryInput = {
  nodes: WorkflowNodeLike[];
  edges: unknown[];
  opcChangeCount: number;
  runStatus?: string | null;
};

export type ActionLike = {
  method: string;
  label: string;
  description: string;
  device_id?: string;
};

export type ActionGroup<T extends ActionLike = ActionLike> = {
  id: string;
  title: string;
  device: string;
  actions: T[];
};

export function formatUiError(error: unknown, action: string) {
  if (error instanceof TypeError && error.message === 'Failed to fetch') {
    return `${action} 失败：无法连接本地调试服务，请确认 workflow_ui 后端仍在运行，且当前页面与后端端口一致。`;
  }
  const message = error instanceof Error ? error.message : String(error);
  return `${action}失败：${message}`;
}

export function buildWorkspaceSummary(input: WorkspaceSummaryInput) {
  const deviceIds = new Set(
    input.nodes
      .map((node) => node.data?.deviceId)
      .filter((deviceId): deviceId is string => Boolean(deviceId)),
  );
  return {
    totalNodes: input.nodes.length,
    totalEdges: input.edges.length,
    runningNodes: input.nodes.filter((node) => node.data?.runStatus === 'running').length,
    completedNodes: input.nodes.filter((node) => node.data?.runStatus === 'success').length,
    deviceCount: deviceIds.size,
    opcChangeCount: input.opcChangeCount,
    runStatusText: runStatusText(input.runStatus),
  };
}

export function groupActionsByDevice<T extends ActionLike>(actions: T[]): ActionGroup<T>[] {
  const actionsByDevice = new Map<string, T[]>();

  actions.forEach((action) => {
    const deviceId = action.device_id || 'unknown_device';
    actionsByDevice.set(deviceId, [...(actionsByDevice.get(deviceId) || []), action]);
  });

  const groups: ActionGroup<T>[] = [];
  actionsByDevice.forEach((groupActions, deviceId) => {
    groups.push({
      id: deviceId,
      title: deviceId === 'unknown_device' ? '其他动作' : deviceId,
      device: deviceId,
      actions: groupActions,
    });
  });
  return groups;
}

function runStatusText(status?: string | null) {
  if (status === 'completed') return '完成';
  if (status === 'failed') return '失败';
  if (status === 'cancelled') return '已终止';
  if (status === 'running') return '运行中';
  if (status === 'preparing') return '准备中';
  if (status === 'pending') return '等待中';
  return '未运行';
}
