export type LogEvent = {
  sequence: number;
  message: string;
  level: string;
  scope: string;
  node_id?: string | null;
  detail?: Record<string, unknown> | null;
};

export type OpcChange = {
  eventSequence: number;
  workflowNodeId: string | null;
  opcNodeId: string;
  displayName: string;
  label: string;
  name: string;
  valueBegin: unknown;
  valueGoal: unknown;
  valueEnd: unknown;
};

export function collectOpcChanges(events: LogEvent[]): OpcChange[] {
  const rows = new Map<string, OpcChange>();

  events.forEach((event) => {
    collectSnapshotRows(event, 'before').forEach((snapshot) => {
      const key = `${event.node_id || 'workflow'}:${snapshot.name}`;
      const current = rows.get(key);
      rows.set(key, {
        ...snapshot,
        ...(current || {}),
        eventSequence: event.sequence,
        workflowNodeId: event.node_id || null,
        valueBegin: snapshot.value,
        valueGoal: snapshot.valueGoal ?? current?.valueGoal,
        valueEnd: current?.valueEnd,
      });
    });

    collectSnapshotRows(event, 'after').forEach((snapshot) => {
      const key = `${event.node_id || 'workflow'}:${snapshot.name}`;
      const current = rows.get(key);
      rows.set(key, {
        ...snapshot,
        ...(current || {}),
        eventSequence: event.sequence,
        workflowNodeId: event.node_id || null,
        valueBegin: current?.valueBegin,
        valueGoal: snapshot.valueGoal ?? current?.valueGoal,
        valueEnd: snapshot.value,
      });
    });

    const changes = event.detail?.changes;
    if (!Array.isArray(changes)) return;

    changes.forEach((change) => {
      if (!isRecord(change) || !('name' in change)) return;
      const name = String(change.name);
      const key = `${event.node_id || 'workflow'}:${name}`;
      const current = rows.get(key);
      rows.set(key, {
        eventSequence: event.sequence,
        workflowNodeId: event.node_id || null,
        opcNodeId: typeof change.node_id === 'string' ? change.node_id : current?.opcNodeId || '',
        displayName:
          typeof change.display_name === 'string' ? change.display_name : current?.displayName || name,
        label: typeof change.label === 'string' ? change.label : current?.label || name,
        name,
        valueBegin: change.before,
        valueGoal: getValueGoal(change, current?.valueGoal),
        valueEnd: change.after,
      });
    });
  });

  return Array.from(rows.values());
}

export function formatOpcValue(value: unknown): string {
  const displayValue = unwrapOpcValue(value);
  if (displayValue === null || displayValue === undefined) return String(displayValue);
  if (typeof displayValue === 'string' || typeof displayValue === 'number' || typeof displayValue === 'boolean') {
    return String(displayValue);
  }
  return JSON.stringify(displayValue);
}

function collectSnapshotRows(event: LogEvent, field: 'before' | 'after') {
  const snapshot = event.detail?.[field];
  if (!isRecord(snapshot)) return [];

  return Object.values(snapshot).flatMap((value) => {
    if (!isRecord(value) || !('name' in value)) return [];
    const name = String(value.name);
    return [
      {
        eventSequence: event.sequence,
        workflowNodeId: event.node_id || null,
        opcNodeId: typeof value.node_id === 'string' ? value.node_id : '',
        displayName: typeof value.display_name === 'string' ? value.display_name : name,
        label: typeof value.label === 'string' ? value.label : name,
        name,
        value: value.value,
        valueGoal: getValueGoal(value),
      },
    ];
  });
}

function getValueGoal(record: Record<string, unknown>, fallback?: unknown): unknown {
  if ('value_goal' in record) return record.value_goal;
  if ('valueGoal' in record) return record.valueGoal;
  if ('goal' in record) return record.goal;
  return fallback;
}

function unwrapOpcValue(value: unknown): unknown {
  if (!isRecord(value)) return value;
  if (value.success === false && typeof value.error === 'string') return value.error;
  if ('value' in value) return value.value;
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object');
}
