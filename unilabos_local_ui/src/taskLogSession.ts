import type { TaskExecutionLogCategory } from './taskActionLog';

export type TaskLogCategory = 'all' | TaskExecutionLogCategory | 'error';

export type TaskLogLine = {
  id: string;
  timestamp: number;
  category: Exclude<TaskLogCategory, 'all'>;
  level: string;
  message: string;
  detail?: Record<string, unknown>;
  executionCategory: TaskExecutionLogCategory;
  code?: string;
  phase?: string;
  seq?: number;
  instanceId?: string;
  sampleId?: string;
  templateId?: string;
  nodeId?: string;
  executionId?: string;
  deviceId?: string;
  actionName?: string;
};

export type TaskWorkspaceLogEvent = {
  id?: string;
  kind: string;
  timestamp: number;
  text: string;
  instanceId?: string;
  sampleId?: string;
  templateId?: string;
  nodeId?: string;
  executionId?: string;
  deviceId?: string;
  actionName?: string;
  category?: TaskExecutionLogCategory;
  level?: string;
  code?: string;
  phase?: string;
  detail?: Record<string, unknown>;
};

export type TaskActionLogForSession = {
  seq: number;
  timestamp: number;
  level: string;
  message: string;
  instance_id: string;
  sample_id: string;
  node_id: string;
  execution_id: string;
  template_id?: string;
  device_id?: string;
  action_name?: string;
  category?: TaskExecutionLogCategory;
  code?: string;
  phase?: string;
  detail: Record<string, unknown>;
};

export type TaskLogSession = {
  startedAt: number;
  actionAfterSeq: number;
};

function isErrorLevel(level: string) {
  return ['ERROR', 'CRITICAL'].includes(level.toUpperCase());
}

function isLegacyError(level: string, message: string) {
  return isErrorLevel(level) || /失败|错误|error|failed/i.test(message);
}

function isOpc(entry: TaskActionLogForSession) {
  return entry.detail.type === 'opc_wait' || /opc|条件满足|变量|传感器/i.test(entry.message);
}

function isResult(entry: TaskActionLogForSession) {
  return entry.detail.type === 'action_result' || /^动作结果[：:]/.test(entry.message);
}

function structuredCategory(category: string | undefined): TaskExecutionLogCategory | null {
  if (category === 'schedule' || category === 'action' || category === 'opc' || category === 'result') {
    return category;
  }
  return null;
}

function legacyActionCategory(entry: TaskActionLogForSession): TaskExecutionLogCategory {
  if (isResult(entry)) return 'result';
  if (isOpc(entry)) return 'opc';
  return 'action';
}

export function buildTaskLogLines({
  events,
  actionEntries,
  session,
}: {
  events: TaskWorkspaceLogEvent[];
  actionEntries: TaskActionLogForSession[];
  session: TaskLogSession;
}): TaskLogLine[] {
  const eventLines = events
    .filter((event) => event.timestamp >= session.startedAt)
    .map((event) => {
      const executionCategory = structuredCategory(event.category) || 'schedule';
      const level = event.level || (isLegacyError('INFO', event.text) ? 'ERROR' : 'INFO');
      const eventIsError = isErrorLevel(level)
        || (!event.level && isLegacyError(level, event.text));
      return {
        id: `event:${event.id || `${event.kind}:${event.timestamp}`}`,
        timestamp: event.timestamp,
        category: eventIsError ? 'error' as const : executionCategory,
        executionCategory,
        level,
        code: event.code,
        phase: event.phase,
        message: event.text,
        detail: event.detail,
        instanceId: event.instanceId,
        sampleId: event.sampleId,
        templateId: event.templateId,
        nodeId: event.nodeId,
        executionId: event.executionId,
        deviceId: event.deviceId,
        actionName: event.actionName,
      };
    });
  const actionLines = actionEntries
    .filter((entry) => entry.seq > session.actionAfterSeq)
    .map((entry) => {
      const explicitCategory = structuredCategory(entry.category);
      const executionCategory = explicitCategory || legacyActionCategory(entry);
      const entryIsError = isErrorLevel(entry.level)
        || (!explicitCategory && isLegacyError(entry.level, entry.message));
      return {
        id: `action:${entry.seq}`,
        timestamp: entry.timestamp,
        category: entryIsError ? 'error' as const : executionCategory,
        executionCategory,
        level: entry.level,
        code: entry.code,
        phase: entry.phase,
        message: entry.message,
        detail: entry.detail,
        seq: entry.seq,
        instanceId: entry.instance_id,
        sampleId: entry.sample_id,
        templateId: entry.template_id,
        nodeId: entry.node_id,
        executionId: entry.execution_id,
        deviceId: entry.device_id,
        actionName: entry.action_name,
      };
    });
  return [...eventLines, ...actionLines].sort((left, right) => left.timestamp - right.timestamp);
}

export function filterTaskLogLines(lines: TaskLogLine[], category: TaskLogCategory) {
  return category === 'all' ? lines : lines.filter((line) => line.category === category);
}
