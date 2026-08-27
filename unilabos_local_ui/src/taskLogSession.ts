export type TaskLogCategory = 'all' | 'schedule' | 'action' | 'opc' | 'result' | 'error';

export type TaskLogLine = {
  id: string;
  timestamp: number;
  category: Exclude<TaskLogCategory, 'all'>;
  level: string;
  message: string;
  detail?: Record<string, unknown>;
  seq?: number;
  instanceId?: string;
  sampleId?: string;
  templateId?: string;
  nodeId?: string;
  executionId?: string;
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
  detail: Record<string, unknown>;
};

export type TaskLogSession = {
  startedAt: number;
  actionAfterSeq: number;
};

function isError(level: string, message: string) {
  return level.toUpperCase() === 'ERROR' || /失败|错误|error|failed/i.test(message);
}

function isOpc(entry: TaskActionLogForSession) {
  return entry.detail.type === 'opc_wait' || /opc|条件满足|变量|传感器/i.test(entry.message);
}

function isResult(entry: TaskActionLogForSession) {
  return entry.detail.type === 'action_result' || /^动作结果[：:]/.test(entry.message);
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
      const eventIsError = isError('INFO', event.text);
      return {
        id: `event:${event.id || `${event.kind}:${event.timestamp}`}`,
        timestamp: event.timestamp,
        category: eventIsError ? 'error' as const : 'schedule' as const,
        level: eventIsError ? 'ERROR' : 'INFO',
        message: event.text,
        detail: event.detail,
        instanceId: event.instanceId,
        sampleId: event.sampleId,
        templateId: event.templateId,
        nodeId: event.nodeId,
        executionId: event.executionId,
      };
    });
  const actionLines = actionEntries
    .filter((entry) => entry.seq > session.actionAfterSeq)
    .map((entry) => ({
      id: `action:${entry.seq}`,
      timestamp: entry.timestamp,
      category: isResult(entry)
        ? 'result' as const
        : isError(entry.level, entry.message) ? 'error' as const
          : isOpc(entry) ? 'opc' as const : 'action' as const,
      level: entry.level,
      message: entry.message,
      detail: entry.detail,
      seq: entry.seq,
      instanceId: entry.instance_id,
      sampleId: entry.sample_id,
      nodeId: entry.node_id,
      executionId: entry.execution_id,
    }));
  return [...eventLines, ...actionLines].sort((left, right) => left.timestamp - right.timestamp);
}

export function filterTaskLogLines(lines: TaskLogLine[], category: TaskLogCategory) {
  return category === 'all' ? lines : lines.filter((line) => line.category === category);
}
