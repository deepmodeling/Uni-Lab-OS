export type TaskExecutionLogCategory = 'schedule' | 'action' | 'opc' | 'result';

export type TaskExecutionLogLevel = 'debug' | 'info' | 'warning' | 'error' | 'critical';

export type TaskActionLogEntry = {
  seq: number;
  timestamp: number;
  instance_id: string;
  node_id: string;
  execution_id: string;
  sample_id: string;
  template_id?: string;
  device_id?: string;
  action_name?: string;
  category?: TaskExecutionLogCategory;
  level: string;
  code?: string;
  phase?: string;
  message: string;
  detail: Record<string, unknown>;
};

export type TaskVariableRow = {
  key: string;
  nodeId: string;
  phase: string;
  variable: string;
  expected: string;
  current: string;
  result: string;
};

export type TaskProcessLogLine = {
  seq: number;
  timestamp: number;
  nodeId: string;
  level: string;
  message: string;
};

type StructuredActionError = {
  code?: unknown;
  error_code?: unknown;
  error_title?: unknown;
  message?: unknown;
  plc_address?: unknown;
  recovery?: unknown;
  station?: unknown;
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function structuredActionError(detail: Record<string, unknown>): StructuredActionError | null {
  const candidates = [detail.reported_failure, detail.result];
  for (const candidate of candidates) {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
    const value = candidate as StructuredActionError;
    if (value.error_code || value.code || value.error_title || value.recovery || value.plc_address) {
      return value;
    }
  }
  return null;
}

function structuredErrorMessage(error: StructuredActionError): string {
  const code = error.error_code || error.code;
  const fields = [
    code ? `报错码：${String(code)}` : '',
    error.station ? `工站：${String(error.station)}` : '',
    error.plc_address ? `PLC 地址：${String(error.plc_address)}` : '',
    error.error_title ? `错误：${String(error.error_title)}` : '',
    error.recovery ? `处理建议：${String(error.recovery)}` : '',
  ].filter(Boolean);
  return fields.join(' · ');
}

function opcWaitPhaseLabel(phase: string | undefined): string {
  switch (phase) {
    case 'start':
      return '检查';
    case 'change':
      return '变化';
    case 'finish':
      return '完成';
    default:
      return phase || '检查';
  }
}

function resultFromOpcWait(detail: Record<string, unknown>, phase: string | undefined): string {
  if (phase === 'finish') {
    if (detail.error) return '错误';
    if (detail.success === true) return '满足';
    if (detail.success === false) return '超时';
  }
  if (detail.satisfied === true) return '满足';
  if (detail.satisfied === false) return '等待';
  const expected = detail.expected;
  const actual = detail.last_value ?? detail.actual;
  if (expected !== undefined && actual !== undefined && actual === expected) return '满足';
  return phase === 'start' ? '检查中' : '等待';
}

function resultForSensorConditionRow(
  parentDetail: Record<string, unknown>,
  row: Record<string, unknown>,
  phase: string | undefined,
): string {
  if (phase === 'finish') {
    return resultFromOpcWait(parentDetail, phase);
  }
  if (row.satisfied === true) return '满足';
  return resultFromOpcWait(row, phase);
}

function currentForSensorConditionRow(
  parentDetail: Record<string, unknown>,
  row: Record<string, unknown>,
  phase: string | undefined,
): string {
  if (phase === 'finish' && parentDetail.success === true) {
    return formatValue(row.expected);
  }
  return formatValue(row.actual);
}

function upsertVariableRow(
  rows: Map<string, TaskVariableRow>,
  nodeId: string,
  variable: string,
  patch: Partial<TaskVariableRow> & Pick<TaskVariableRow, 'expected' | 'current' | 'result' | 'phase'>,
) {
  const key = `${nodeId}:${variable}`;
  const previous = rows.get(key);
  rows.set(key, {
    key,
    nodeId,
    phase: patch.phase || previous?.phase || '检查',
    variable,
    expected: patch.expected ?? previous?.expected ?? '—',
    current: patch.current ?? previous?.current ?? '—',
    result: patch.result ?? previous?.result ?? '等待',
  });
}

function absorbOpcWaitDetail(
  rows: Map<string, TaskVariableRow>,
  nodeId: string,
  detail: Record<string, unknown>,
) {
  const phase = typeof detail.phase === 'string' ? detail.phase : undefined;
  if (detail.wait_kind === 'sensor_conditions' && Array.isArray(detail.conditions)) {
    for (const item of detail.conditions) {
      if (!item || typeof item !== 'object') continue;
      const row = item as Record<string, unknown>;
      const variable = String(row.display_name || row.variable || '传感器');
      upsertVariableRow(rows, nodeId, variable, {
        phase: typeof detail.context === 'string' ? detail.context : opcWaitPhaseLabel(phase),
        expected: formatValue(row.expected),
        current: currentForSensorConditionRow(detail, row, phase),
        result: resultForSensorConditionRow(detail, row, phase),
      });
    }
    return;
  }
  const variable = String(detail.display_name || detail.variable || detail.label || '变量');
  const expected = formatValue(detail.expected);
  const current = formatValue(
    detail.last_value ?? detail.actual ?? detail.previous_value,
  );
  upsertVariableRow(rows, nodeId, variable, {
    phase: opcWaitPhaseLabel(phase),
    expected,
    current,
    result: resultFromOpcWait(detail, phase),
  });
}

export function buildTaskVariableRows(entries: TaskActionLogEntry[]): TaskVariableRow[] {
  const rows = new Map<string, TaskVariableRow>();
  for (const entry of entries) {
    const detail = entry.detail || {};
    if (detail.type === 'opc_wait') {
      absorbOpcWaitDetail(rows, entry.node_id, detail);
    }
  }
  return Array.from(rows.values());
}

export function buildTaskProcessLogLines(entries: TaskActionLogEntry[]): TaskProcessLogLine[] {
  return entries.flatMap((entry) => {
      const detail = entry.detail || {};
      if (detail.type === 'opc_wait') return [];
      const lines: TaskProcessLogLine[] = [{
      seq: entry.seq,
      timestamp: entry.timestamp,
      nodeId: entry.node_id,
      level: entry.level,
      message: entry.message,
      }];
      const structured = structuredActionError(detail);
      if (structured) {
        const message = structuredErrorMessage(structured);
        if (message) {
          lines.push({
            seq: entry.seq,
            timestamp: entry.timestamp,
            nodeId: entry.node_id,
            level: 'error',
            message,
          });
        }
      }
      return lines;
    });
}

export function mergeTaskActionLogs(
  previous: TaskActionLogEntry[],
  incoming: TaskActionLogEntry[],
): TaskActionLogEntry[] {
  const bySeq = new Map<number, TaskActionLogEntry>();
  for (const entry of [...previous, ...incoming]) {
    bySeq.set(entry.seq, entry);
  }
  return Array.from(bySeq.values())
    .sort((left, right) => left.seq - right.seq);
}

export function groupTaskActionLogsByNode(
  entries: TaskActionLogEntry[],
  nodeIds: string[],
): Array<{ nodeId: string; entries: TaskActionLogEntry[] }> {
  const byNode = new Map<string, TaskActionLogEntry[]>();
  for (const entry of entries) {
    const bucket = byNode.get(entry.node_id) || [];
    bucket.push(entry);
    byNode.set(entry.node_id, bucket);
  }
  const ordered = [...nodeIds];
  for (const nodeId of byNode.keys()) {
    if (!ordered.includes(nodeId)) {
      ordered.push(nodeId);
    }
  }
  if (!ordered.length) {
    return Array.from(byNode.entries())
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([nodeId, nodeEntries]) => ({ nodeId, entries: nodeEntries }));
  }
  return ordered
    .filter((nodeId) => byNode.has(nodeId))
    .map((nodeId) => ({ nodeId, entries: byNode.get(nodeId) || [] }));
}

export async function fetchTaskActionLogs(
  fetcher: typeof fetch,
  workflowPath: string,
  options: { afterSeq?: number; instanceId?: string } = {},
): Promise<{
  latest_seq: number;
  next_after_seq: number;
  has_more: boolean;
  entries: TaskActionLogEntry[];
}> {
  const requestedAfterSeq = options.afterSeq ?? 0;
  const params = new URLSearchParams({
    task_workspace_path: workflowPath,
    after_seq: String(requestedAfterSeq),
  });
  if (options.instanceId) {
    params.set('instance_id', options.instanceId);
  }
  const response = await fetcher(`/api/task-execution/logs?${params.toString()}`);
  const payload = await response.json() as {
    success?: boolean;
    latest_seq?: number;
    next_after_seq?: number;
    has_more?: boolean;
    entries?: TaskActionLogEntry[];
    message?: string;
  };
  if (!response.ok || !payload.success) {
    throw new Error(payload.message || 'Task 日志不可用');
  }
  const entries = Array.isArray(payload.entries) ? payload.entries : [];
  const lastEntrySeq = entries.length ? Number(entries[entries.length - 1].seq) : requestedAfterSeq;
  const nextAfterSeq = Number(payload.next_after_seq ?? lastEntrySeq);
  const hasMore = payload.has_more === true;
  if (hasMore && nextAfterSeq <= requestedAfterSeq) {
    throw new Error('Task 日志分页游标未推进');
  }
  return {
    latest_seq: Number(payload.latest_seq || 0),
    next_after_seq: nextAfterSeq,
    has_more: hasMore,
    entries,
  };
}

export async function fetchAllTaskActionLogs(
  fetcher: typeof fetch,
  workflowPath: string,
  options: { afterSeq?: number; instanceId?: string } = {},
): Promise<{
  latest_seq: number;
  next_after_seq: number;
  entries: TaskActionLogEntry[];
}> {
  let afterSeq = options.afterSeq ?? 0;
  let latestSeq = afterSeq;
  const entries: TaskActionLogEntry[] = [];
  while (true) {
    const page = await fetchTaskActionLogs(fetcher, workflowPath, {
      ...options,
      afterSeq,
    });
    latestSeq = Math.max(latestSeq, page.latest_seq);
    entries.push(...page.entries);
    afterSeq = page.next_after_seq;
    if (!page.has_more) {
      return { latest_seq: latestSeq, next_after_seq: afterSeq, entries };
    }
  }
}
