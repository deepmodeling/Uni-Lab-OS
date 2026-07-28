export type TaskLogCategory = 'all' | 'schedule' | 'action' | 'opc' | 'error';

export type TaskLogLine = {
  id: string;
  timestamp: number;
  category: Exclude<TaskLogCategory, 'all'>;
  level: string;
  message: string;
};

export type TaskWorkspaceLogEvent = {
  kind: string;
  timestamp: number;
  text: string;
};

export type TaskActionLogForSession = {
  seq: number;
  timestamp: number;
  level: string;
  message: string;
  node_id: string;
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
    .map((event) => ({
      id: `event:${event.kind}:${event.timestamp}`,
      timestamp: event.timestamp,
      category: isError('INFO', event.text) ? 'error' as const : 'schedule' as const,
      level: event.kind,
      message: event.text,
    }));
  const actionLines = actionEntries
    .filter((entry) => entry.seq > session.actionAfterSeq)
    .map((entry) => ({
      id: `action:${entry.seq}`,
      timestamp: entry.timestamp,
      category: isError(entry.level, entry.message)
        ? 'error' as const
        : isOpc(entry) ? 'opc' as const : 'action' as const,
      level: entry.level,
      message: entry.message,
    }));
  return [...eventLines, ...actionLines].sort((left, right) => left.timestamp - right.timestamp);
}

export function filterTaskLogLines(lines: TaskLogLine[], category: TaskLogCategory) {
  return category === 'all' ? lines : lines.filter((line) => line.category === category);
}
