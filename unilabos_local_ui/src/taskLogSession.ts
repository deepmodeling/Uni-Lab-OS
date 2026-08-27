import type { TaskExecutionLogCategory } from './taskActionLog';

export type TaskLogCategory = 'all' | TaskExecutionLogCategory | 'error';
export type TaskErrorStateFilter = 'all' | 'active' | 'recovered';

export type TaskLogLine = {
  id: string;
  timestamp: number;
  category: TaskExecutionLogCategory;
  isError: boolean;
  errorState?: 'active' | 'recovered';
  recoveredAt?: number;
  recoveredByLineId?: string;
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

const TASK_LOG_CATEGORY_LABELS: Record<TaskExecutionLogCategory, string> = {
  schedule: '调度',
  action: 'Action',
  opc: 'OPC',
  result: '结果',
};

const TASK_LOG_PHASE_LABELS: Record<string, string> = {
  preparing: '准备',
  executing: '执行',
  completed: '完成',
  scheduling: '调度循环',
  dispatching: '派发',
  connecting: '连接',
  registering: '注册',
  polling: '轮询',
  sampling_before: '动作前采样',
  sampling_live: '动作中采样',
  sampling_after: '动作后采样',
  start: '开始',
  finish: '结束',
  recovered: '已恢复',
};

export function taskLogCategoryLabel(category: TaskExecutionLogCategory) {
  return TASK_LOG_CATEGORY_LABELS[category];
}

export function taskLogPhaseLabel(phase: string | undefined) {
  if (!phase) return '';
  return TASK_LOG_PHASE_LABELS[phase] || phase;
}

export function isTaskLogRecovery(line: Pick<TaskLogLine, 'code' | 'phase'>) {
  return line.phase === 'recovered' || Boolean(line.code?.endsWith('_recovered'));
}

export function formatTaskLogMetadata(
  line: Pick<
    TaskLogLine,
    'category' | 'level' | 'code' | 'phase' | 'errorState' | 'recoveredAt'
  >,
) {
  return [
    `category=${line.category}`,
    `level=${line.level.toLowerCase()}`,
    line.code ? `code=${line.code}` : '',
    line.phase ? `phase=${line.phase}` : '',
    line.errorState ? `state=${line.errorState}` : '',
    line.recoveredAt !== undefined ? `recovered_at=${line.recoveredAt}` : '',
  ].filter(Boolean).map((item) => `[${item}]`).join(' ');
}

function recoveryOriginalCode(line: TaskLogLine) {
  const originalCode = line.detail?.original_code;
  if (typeof originalCode === 'string' && originalCode) return originalCode;
  if (line.code === 'opc_snapshot_recovered') return 'opc_snapshot_read_failed';
  return '';
}

function recoveryMatchesError(errorLine: TaskLogLine, recoveryLine: TaskLogLine) {
  if (errorLine.executionCategory !== recoveryLine.executionCategory) return false;
  const originalCode = recoveryOriginalCode(recoveryLine);
  if (!originalCode || errorLine.code !== originalCode) return false;
  const contextKeys = [
    'instanceId',
    'nodeId',
    'executionId',
    'deviceId',
    'actionName',
  ] as const;
  return contextKeys.every((key) => {
    const recoveryValue = recoveryLine[key];
    return !recoveryValue || errorLine[key] === recoveryValue;
  });
}

function linkTaskLogRecoveries(lines: TaskLogLine[]) {
  for (const line of lines) {
    if (line.isError) line.errorState = 'active';
    if (!isTaskLogRecovery(line)) continue;
    const errorLine = [...lines]
      .reverse()
      .find((candidate) => (
        candidate.timestamp <= line.timestamp
        && candidate.errorState === 'active'
        && recoveryMatchesError(candidate, line)
      ));
    if (!errorLine) continue;
    errorLine.errorState = 'recovered';
    errorLine.recoveredAt = line.timestamp;
    errorLine.recoveredByLineId = line.id;
  }
  return lines;
}

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
        category: executionCategory,
        isError: eventIsError,
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
        category: executionCategory,
        isError: entryIsError,
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
  return linkTaskLogRecoveries(
    [...eventLines, ...actionLines].sort((left, right) => left.timestamp - right.timestamp),
  );
}

export function filterTaskLogLines(lines: TaskLogLine[], category: TaskLogCategory) {
  if (category === 'all') return lines;
  if (category === 'error') return lines.filter((line) => line.isError);
  return lines.filter((line) => line.executionCategory === category);
}

export function filterTaskErrorLines(
  lines: TaskLogLine[],
  state: TaskErrorStateFilter,
) {
  const errors = filterTaskLogLines(lines, 'error');
  if (state === 'all') return errors;
  return errors.filter((line) => line.errorState === state);
}

export function countTaskErrorStates(lines: TaskLogLine[]) {
  const counts = { total: 0, active: 0, recovered: 0 };
  for (const line of lines) {
    if (!line.isError) continue;
    counts.total += 1;
    if (line.errorState === 'recovered') counts.recovered += 1;
    else counts.active += 1;
  }
  return counts;
}

export function countTaskLogCategories(lines: TaskLogLine[]) {
  const counts = {
    all: lines.length,
    schedule: 0,
    action: 0,
    opc: 0,
    result: 0,
    error: 0,
    activeErrors: 0,
  };
  for (const line of lines) {
    counts[line.executionCategory] += 1;
    if (!line.isError) continue;
    counts.error += 1;
    if (line.errorState !== 'recovered') counts.activeErrors += 1;
  }
  return counts;
}
