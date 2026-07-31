export type CsvVariableModel = {
  name: string;
  data_type: string;
  initial_value: string;
  plcDeviceId?: string;
  display_name?: string;
  aliases?: string[];
};

export type TriggerCondition = {
  plcDeviceId?: string;
  variableName: string;
  dataType: string;
  value: string | number | boolean;
};

export type TaskTemplateModel = {
  id: string;
  name: string;
  nodeIds: string[];
  resources: string[];
  gates: string[];
  inputTriggers?: TriggerCondition[];
  outputTriggers?: TriggerCondition[];
};

export type TaskNodeDescriptor = {
  id: string;
  deviceId?: string;
  method?: string;
  opcVariables?: string[];
};

export type ResolvedTemplateNode<T extends TaskNodeDescriptor = TaskNodeDescriptor> = {
  templateNodeId: string;
  node: T | null;
};

export function inferMethodFromTemplateNodeId(nodeId: string) {
  const match = /^node_\d+_(.+)$/.exec(nodeId.trim());
  return match?.[1] || null;
}

export function resolveTemplateNodes<T extends TaskNodeDescriptor>(
  nodeIds: string[],
  nodes: T[],
): ResolvedTemplateNode<T>[] {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const nodesByMethod = new Map<string, T[]>();
  for (const node of nodes) {
    const method = node.method?.trim();
    if (!method) continue;
    const bucket = nodesByMethod.get(method) || [];
    bucket.push(node);
    nodesByMethod.set(method, bucket);
  }

  const usedNodeIds = new Set<string>();
  return nodeIds.map((templateNodeId) => {
    const exact = nodesById.get(templateNodeId);
    if (exact && !usedNodeIds.has(exact.id)) {
      usedNodeIds.add(exact.id);
      return { templateNodeId, node: exact };
    }

    const method = inferMethodFromTemplateNodeId(templateNodeId) || templateNodeId;
    const candidates = (nodesByMethod.get(method) || []).filter((node) => !usedNodeIds.has(node.id));
    if (candidates.length) {
      usedNodeIds.add(candidates[0].id);
      return { templateNodeId, node: candidates[0] };
    }

    return { templateNodeId, node: null };
  });
}

export type TaskGanttEntry = {
  id: string;
  instanceId: string;
  sample: string;
  templateId: string;
  templateName: string;
  resource: string;
  startAt: number;
  endAt: number;
  state: 'planned' | 'running' | 'done';
  involvedDeviceIds?: string[];
  nodeInfoIncomplete?: boolean;
};

export type ApiTaskGanttEntry = {
  instance_id: string;
  template_id: string;
  sample_id: string;
  start_at: number;
  end_at: number;
  resources: string[];
  state: 'planned' | 'running' | 'done';
};

export type SampleProcessBlockState =
  | 'waiting'
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type SampleProcessBlock = {
  id: string;
  instanceId: string;
  sample: string;
  templateId: string;
  templateName: string;
  order: number;
  state: SampleProcessBlockState;
  actionDone: number;
  actionTotal: number;
  actions: TaskActionProgress[];
  totalDurationMs: number | null;
};

export type SampleProcessRow = {
  sample: string;
  blocks: SampleProcessBlock[];
};

export type SampleProcessRowStatus =
  | 'current'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'queued';

export type TaskInstanceProcessInput = {
  id: string;
  sample: string;
  templateId: string;
  order: number;
  status: string;
  executionCursor?: number;
  actionRecords?: TaskActionExecutionRecord[];
  startedAt?: number;
  finishedAt?: number;
};

export type TaskActionExecutionRecord = {
  nodeId: string;
  attempt: number;
  executionId: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed';
  startedAt?: number;
  finishedAt?: number;
};

export type TaskActionAttemptTiming = TaskActionExecutionRecord & {
  durationMs: number | null;
};

export type TaskActionProgress = {
  nodeId: string;
  index: number;
  state: 'waiting' | 'running' | 'completed' | 'failed';
  durationMs: number | null;
  attempts: TaskActionAttemptTiming[];
};

export function elapsedDurationMs(
  startedAt: number | null | undefined,
  finishedAt: number | null | undefined,
  nowMs = Date.now(),
) {
  if (startedAt == null || !Number.isFinite(startedAt)) return null;
  const endAt = finishedAt == null ? nowMs : finishedAt;
  if (!Number.isFinite(endAt)) return null;
  return Math.max(0, endAt - startedAt);
}

export function taskWallDurationMs(
  task: Pick<TaskInstanceProcessInput, 'status' | 'actionRecords'> & {
    startedAt?: number;
    finishedAt?: number;
  },
  nowMs = Date.now(),
) {
  const records = task.actionRecords || [];
  const actionStartedAt = records
    .map((record) => record.startedAt)
    .filter((value): value is number => value != null && Number.isFinite(value));
  if (!actionStartedAt.length) return null;
  const startedAt = Math.min(...actionStartedAt);
  const terminal = task.status === 'completed'
    || task.status === 'failed'
    || task.status === 'cancelled';
  const actionFinishedAt = records
    .map((record) => record.finishedAt)
    .filter((value): value is number => value != null && Number.isFinite(value));
  const finishedAt = actionFinishedAt.length
    ? Math.max(...actionFinishedAt)
    : task.finishedAt;
  if (terminal && finishedAt == null) return null;
  return elapsedDurationMs(
    startedAt,
    terminal ? finishedAt : undefined,
    nowMs,
  );
}

export function formatElapsedDurationMs(durationMs: number | null | undefined) {
  if (durationMs == null || !Number.isFinite(durationMs)) return '—';
  const totalSeconds = Math.max(0, Math.floor(durationMs / 1_000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
}

export function taskActionProgressMinWidth(actionTotal: number) {
  return Math.max(220, Math.max(0, Math.floor(actionTotal)) * 140);
}

function actionAttemptStateLabel(status: TaskActionExecutionRecord['status']) {
  if (status === 'running') return '执行中';
  if (status === 'succeeded') return '已完成';
  if (status === 'failed') return '失败';
  return '待执行';
}

function formatActionClockTime(timestamp: number | undefined) {
  if (timestamp == null) return '—';
  return new Date(timestamp).toLocaleTimeString('zh-CN', { hour12: false });
}

export function formatTaskActionTimingTitle(action: TaskActionProgress, label: string) {
  const state = action.state === 'running'
    ? '执行中'
    : action.state === 'completed'
      ? '已完成'
      : action.state === 'failed'
        ? '失败'
        : '待执行';
  const header = `${action.index + 1}. ${label} · ${state}`;
  if (!action.attempts.length) return header;
  const attempts = action.attempts.map((attempt) => (
    `尝试 ${attempt.attempt} · ${actionAttemptStateLabel(attempt.status)} · `
    + `${formatActionClockTime(attempt.startedAt)} → ${attempt.finishedAt == null ? '现在' : formatActionClockTime(attempt.finishedAt)}`
    + ` · ${formatElapsedDurationMs(attempt.durationMs)}`
  ));
  return [header, ...attempts].join('\n');
}

export function buildTaskActionProgress(
  nodeIds: string[],
  records: TaskActionExecutionRecord[],
  nowMs = Date.now(),
): TaskActionProgress[] {
  const recordsByNodeId = new Map<string, TaskActionExecutionRecord[]>();
  for (const record of records) {
    const bucket = recordsByNodeId.get(record.nodeId) || [];
    bucket.push(record);
    recordsByNodeId.set(record.nodeId, bucket);
  }

  return nodeIds.map((nodeId, index) => {
    const attempts = [...(recordsByNodeId.get(nodeId) || [])]
      .sort((left, right) => (
        left.attempt - right.attempt
        || (left.startedAt ?? Number.MAX_SAFE_INTEGER) - (right.startedAt ?? Number.MAX_SAFE_INTEGER)
      ))
      .map((attempt) => ({
        ...attempt,
        durationMs: elapsedDurationMs(attempt.startedAt, attempt.finishedAt, nowMs),
      }));
    const latest = attempts[attempts.length - 1];
    const succeeded = [...attempts].reverse().find((attempt) => attempt.status === 'succeeded');
    const state = succeeded
      ? 'completed'
      : latest?.status === 'running'
        ? 'running'
        : latest?.status === 'failed'
          ? 'failed'
          : 'waiting';
    const visibleAttempt = succeeded || latest;
    return {
      nodeId,
      index,
      state,
      durationMs: visibleAttempt?.durationMs ?? null,
      attempts,
    };
  });
}

function blockVisualState(
  status: string,
  actions: TaskActionProgress[],
): SampleProcessBlockState {
  if (
    status === 'running'
    && !actions.some((action) => action.state !== 'waiting')
  ) {
    return 'pending';
  }
  if (status === 'waiting'
    || status === 'pending'
    || status === 'running'
    || status === 'completed'
    || status === 'failed'
    || status === 'cancelled') {
    return status;
  }
  return 'waiting';
}

export function sampleProcessRowStatus(
  blocks: Array<Pick<SampleProcessBlock, 'state'>>,
): SampleProcessRowStatus {
  if (blocks.some((block) => block.state === 'running')) return 'current';
  if (blocks.length > 0 && blocks.every((block) => block.state === 'completed')) return 'completed';
  if (blocks.some((block) => block.state === 'failed')) return 'failed';
  if (blocks.some((block) => block.state === 'cancelled')) return 'cancelled';
  return 'queued';
}

export function buildSampleProcessRows(
  instances: TaskInstanceProcessInput[],
  templates: Array<Pick<TaskTemplateModel, 'id' | 'name' | 'nodeIds'>>,
  nowMs = Date.now(),
): SampleProcessRow[] {
  const templatesById = new Map(templates.map((template) => [template.id, template]));
  const bySample = new Map<string, SampleProcessBlock[]>();
  for (const instance of instances) {
    const template = templatesById.get(instance.templateId);
    const actionTotal = template?.nodeIds.length || 0;
    const cursor = Math.max(0, Number(instance.executionCursor || 0));
    const actionDone = instance.status === 'completed'
      ? actionTotal
      : Math.min(cursor, actionTotal);
    const actions = buildTaskActionProgress(
      template?.nodeIds || [],
      instance.actionRecords || [],
      nowMs,
    );
    const block: SampleProcessBlock = {
      id: instance.id,
      instanceId: instance.id,
      sample: instance.sample,
      templateId: instance.templateId,
      templateName: template?.name || instance.templateId,
      order: instance.order,
      state: blockVisualState(instance.status, actions),
      actionDone,
      actionTotal,
      actions,
      totalDurationMs: taskWallDurationMs(instance, nowMs),
    };
    const bucket = bySample.get(instance.sample) || [];
    bucket.push(block);
    bySample.set(instance.sample, bucket);
  }
  return Array.from(bySample.entries())
    .sort(([left], [right]) => left.localeCompare(right, 'zh-CN'))
    .map(([sample, blocks]) => ({
      sample,
      blocks: blocks.sort((left, right) => left.order - right.order || left.templateId.localeCompare(right.templateId)),
    }));
}

export function buildTaskGanttEntries(
  entries: ApiTaskGanttEntry[],
): Array<Omit<TaskGanttEntry, 'templateName'>> {
  return entries.map((entry) => {
    const resource = `task:${entry.template_id}`;
    return {
      id: `${entry.instance_id}:${resource}`,
      instanceId: entry.instance_id,
      sample: entry.sample_id,
      templateId: entry.template_id,
      resource,
      startAt: entry.start_at,
      endAt: entry.end_at,
      state: entry.state,
    };
  });
}

export function annotateTaskGanttEntries<T extends Omit<TaskGanttEntry, 'templateName'>>(
  entries: T[],
  templates: Array<Pick<TaskTemplateModel, 'id' | 'nodeIds'>>,
  nodes: TaskNodeDescriptor[],
) {
  const templatesById = new Map(templates.map((template) => [template.id, template]));
  return entries.map((entry) => {
    const template = templatesById.get(entry.templateId);
    const templateNodes = template
      ? resolveTemplateNodes(template.nodeIds, nodes).map((resolved) => resolved.node)
      : [];
    return {
      ...entry,
      involvedDeviceIds: Array.from(new Set(
        templateNodes
          .map((node) => node?.deviceId)
          .filter((deviceId): deviceId is string => Boolean(deviceId)),
      )),
      nodeInfoIncomplete: !template
        || templateNodes.length !== template.nodeIds.length
        || templateNodes.some((node) => !node),
    };
  });
}

export function createSynchronousActionGate() {
  let inFlight = false;
  return {
    tryStart() {
      if (inFlight) return false;
      inFlight = true;
      return true;
    },
    finish() {
      inFlight = false;
    },
    isInFlight() {
      return inFlight;
    },
  };
}

export function createOperationGenerationController() {
  let generation = 0;
  return {
    begin() {
      return ++generation;
    },
    isCurrent(candidate: number) {
      return candidate === generation;
    },
    invalidate() {
      generation += 1;
    },
  };
}

export function createTaskTemplateId(
  sequence: number,
  randomUUID: () => string = () => globalThis.crypto.randomUUID(),
) {
  return `task_${sequence.toString(36)}_${randomUUID().replace(/-/g, '')}`;
}

export function resolveTaskTemplateNameDraft(draft: string, currentName: string) {
  return draft.trim() || currentName;
}

export function updateScheduledTemplateDraft(
  current: string[],
  templateId: string,
  operation: 'add' | 'remove',
) {
  if (operation === 'add') {
    return current.includes(templateId) ? current : [...current, templateId];
  }
  return current.includes(templateId)
    ? current.filter((id) => id !== templateId)
    : current;
}

/** output_triggers 只描述完成后的输出动作，不门控 Task 完成；waiting 仅含输入阶段的 waiting/pending。 */
export function isTaskWaitingStatus(status: string) {
  return status === 'waiting' || status === 'pending';
}

export function canDeleteTaskTemplate(
  templateId: string,
  instances: Array<{ templateId: string; status: string }>,
  state: { schedulerBusy: boolean; actionInFlight: boolean },
) {
  const terminalStatuses = new Set(['completed', 'failed', 'cancelled', 'done']);
  const hasActiveInstance = instances.some((instance) => (
    instance.templateId === templateId && !terminalStatuses.has(instance.status)
  ));
  return !hasActiveInstance && !state.schedulerBusy && !state.actionInFlight;
}

export type WorkspaceEpoch = {
  path: string;
  generation: number;
  signal: AbortSignal;
};

export function createWorkspaceEpochController() {
  let generation = 0;
  let current: WorkspaceEpoch | null = null;
  let controller: AbortController | null = null;
  return {
    begin(path: string): WorkspaceEpoch {
      controller?.abort();
      controller = new AbortController();
      current = { path, generation: ++generation, signal: controller.signal };
      return current;
    },
    isCurrent(epoch: Pick<WorkspaceEpoch, 'path' | 'generation'>) {
      return current?.path === epoch.path && current.generation === epoch.generation;
    },
    current() {
      return current;
    },
    abort() {
      controller?.abort();
    },
  };
}

export function renameTaskTemplate<T extends TaskTemplateModel>(
  templates: T[],
  templateId: string,
  name: string,
) {
  const nextName = name.trim();
  if (!nextName) return templates;
  return templates.map((template) => (
    template.id === templateId ? { ...template, name: nextName } : template
  ));
}

export function createTaskTemplateDraft(
  id: string,
  name: string,
  nodes: TaskNodeDescriptor[],
): TaskTemplateModel {
  return {
    id,
    name,
    nodeIds: nodes.map((node) => node.id),
    resources: [],
    gates: [],
    inputTriggers: [],
    outputTriggers: [],
  };
}

export function taskTemplateDeviceIds(
  template: Pick<TaskTemplateModel, 'nodeIds'>,
  nodes: TaskNodeDescriptor[],
) {
  return Array.from(new Set(
    resolveTemplateNodes(template.nodeIds, nodes)
      .map((resolved) => resolved.node?.deviceId)
      .filter((deviceId): deviceId is string => Boolean(deviceId)),
  ));
}

export function taskLocalWaitingReason(
  task: { sample: string; templateId: string; order: number; status: string },
  instances: Array<{ sample: string; order: number; status: string }>,
  templates: Array<Pick<TaskTemplateModel, 'id'>>,
) {
  if (!isTaskWaitingStatus(task.status)) return '';
  if (!templates.some((template) => template.id === task.templateId)) {
    return '缺少 Task 模板';
  }
  const previousDone = instances
    .filter((instance) => instance.sample === task.sample && instance.order < task.order)
    .every((instance) => instance.status === 'done' || instance.status === 'completed');
  return previousDone ? '' : '同一样品的前序 Task 未完成';
}

function normalizedDataType(dataType: string) {
  return dataType.trim().toUpperCase();
}

export function createDefaultTriggerCondition(variable?: CsvVariableModel): TriggerCondition {
  const dataType = normalizedDataType(variable?.data_type || 'STRING');
  const initialValue = variable?.initial_value ?? '';
  const identity = {
    ...(variable?.plcDeviceId ? { plcDeviceId: variable.plcDeviceId } : {}),
    variableName: variable?.name || '',
    dataType,
  };
  if (dataType === 'BOOL' || dataType === 'BOOLEAN') {
    return {
      ...identity,
      value: initialValue.trim().toLowerCase() === 'true',
    };
  }
  if (dataType === 'INTEGER' || dataType === 'INT' || dataType === 'FLOAT' || dataType === 'DOUBLE' || dataType === 'NUMBER') {
    const numericValue = Number(initialValue);
    return {
      ...identity,
      value: Number.isFinite(numericValue) ? numericValue : 0,
    };
  }
  return { ...identity, value: initialValue };
}

export function normalizeTriggerConditions(
  conditions: TriggerCondition[],
  csvVariables: CsvVariableModel[],
): TriggerCondition[] {
  if (!csvVariables.length) {
    return conditions.length ? conditions : [];
  }
  const normalized = conditions.flatMap((condition) => {
    if (
      condition.plcDeviceId
      && !csvVariables.some((variable) => variable.plcDeviceId === condition.plcDeviceId)
    ) {
      return [condition];
    }
    const candidates = csvVariables.filter((variable) => (
      variable.name === condition.variableName
      && (!condition.plcDeviceId || variable.plcDeviceId === condition.plcDeviceId)
    ));
    if (candidates.length !== 1) return [];
    const variable = candidates[0];
    return [{
      ...(variable.plcDeviceId ? { plcDeviceId: variable.plcDeviceId } : {}),
      variableName: variable.name,
      dataType: normalizedDataType(variable.data_type),
      value: condition.value,
    }];
  });
  return normalized;
}

export function updateTaskTemplateTriggers<T extends TaskTemplateModel>(
  templates: T[],
  templateId: string,
  kind: 'input' | 'output',
  triggers: TriggerCondition[],
  csvVariables: CsvVariableModel[] = [],
) {
  const normalized = normalizeTriggerConditions(triggers, csvVariables);
  const field = kind === 'input' ? 'inputTriggers' : 'outputTriggers';
  return templates.map((template) => (
    template.id === templateId ? { ...template, [field]: normalized } : template
  ));
}
