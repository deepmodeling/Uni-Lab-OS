export type TaskNodeParameters = Record<string, Record<string, unknown>>;
export type TaskParameterSchema = Record<string, Record<string, readonly string[]>>;

export type TaskTestMemory = {
  version: 1;
  sampleCount: number;
  templateParameters: Record<string, TaskNodeParameters>;
};

type StorageReader = Pick<Storage, 'getItem'>;
type StorageWriter = Pick<Storage, 'setItem'>;

const TASK_TEST_MEMORY_PREFIX = 'unilabos.taskTestMemory.v1';
const DEFAULT_SAMPLE_COUNT = 3;

export function createEmptyTaskTestMemory(): TaskTestMemory {
  return {
    version: 1,
    sampleCount: DEFAULT_SAMPLE_COUNT,
    templateParameters: {},
  };
}

export function taskTestMemoryKey(workflowPath: string) {
  return `${TASK_TEST_MEMORY_PREFIX}.${workflowPath || 'default'}`;
}

export function loadTaskTestMemory(storage: StorageReader, workflowPath: string): TaskTestMemory {
  try {
    const raw = storage.getItem(taskTestMemoryKey(workflowPath));
    if (!raw) return createEmptyTaskTestMemory();
    const parsed = JSON.parse(raw) as unknown;
    if (!isRecord(parsed) || parsed.version !== 1 || !isRecord(parsed.templateParameters)) {
      return createEmptyTaskTestMemory();
    }
    const templateParameters = Object.fromEntries(
      Object.entries(parsed.templateParameters).flatMap(([templateId, parameters]) => {
        const normalized = normalizeNodeParameters(parameters);
        return templateId && normalized ? [[templateId, normalized]] : [];
      }),
    );
    return {
      version: 1,
      sampleCount: normalizeSampleCount(parsed.sampleCount),
      templateParameters,
    };
  } catch {
    return createEmptyTaskTestMemory();
  }
}

export function saveTaskTestMemory(
  storage: StorageWriter,
  workflowPath: string,
  memory: TaskTestMemory,
) {
  storage.setItem(taskTestMemoryKey(workflowPath), JSON.stringify(memory));
}

export function withTaskSampleCount(memory: TaskTestMemory, sampleCount: number): TaskTestMemory {
  return { ...memory, sampleCount: normalizeSampleCount(sampleCount) };
}

export function withRememberedTemplateParameters(
  memory: TaskTestMemory,
  templateId: string,
  nodeParameters: TaskNodeParameters,
): TaskTestMemory {
  if (!templateId) return memory;
  if (!Object.keys(nodeParameters).length) {
    const { [templateId]: _removed, ...remaining } = memory.templateParameters;
    return { ...memory, templateParameters: remaining };
  }
  return {
    ...memory,
    templateParameters: {
      ...memory.templateParameters,
      [templateId]: cloneNodeParameters(nodeParameters),
    },
  };
}

export function withoutRememberedTemplateParameters(memory: TaskTestMemory): TaskTestMemory {
  return { ...memory, templateParameters: {} };
}

export function rememberedParametersForTemplates(
  memory: TaskTestMemory,
  templateIds: string[],
  schema?: TaskParameterSchema,
) {
  const selected = new Set(templateIds);
  return Object.fromEntries(
    Object.entries(memory.templateParameters)
      .filter(([templateId]) => selected.has(templateId))
      .flatMap(([templateId, parameters]) => {
        const filtered = schema
          ? filterNodeParameters(parameters, schema[templateId] || {})
          : cloneNodeParameters(parameters);
        return Object.keys(filtered).length ? [[templateId, filtered]] : [];
      }),
  );
}

function filterNodeParameters(
  parameters: TaskNodeParameters,
  schema: Record<string, readonly string[]>,
) {
  return Object.fromEntries(
    Object.entries(parameters).flatMap(([nodeId, values]) => {
      const allowedNames = schema[nodeId];
      if (!allowedNames) return [];
      const allowed = new Set(allowedNames);
      const filteredValues = Object.fromEntries(
        Object.entries(values).filter(([name]) => allowed.has(name)),
      );
      return Object.keys(filteredValues).length ? [[nodeId, structuredCloneValue(filteredValues)]] : [];
    }),
  );
}

function normalizeNodeParameters(value: unknown): TaskNodeParameters | null {
  if (!isRecord(value)) return null;
  const entries = Object.entries(value).flatMap(([nodeId, parameters]) => (
    nodeId && isRecord(parameters) ? [[nodeId, { ...parameters }]] : []
  ));
  return Object.fromEntries(entries);
}

function cloneNodeParameters(parameters: TaskNodeParameters): TaskNodeParameters {
  return Object.fromEntries(
    Object.entries(parameters).map(([nodeId, values]) => [nodeId, structuredCloneValue(values)]),
  );
}

function structuredCloneValue<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function normalizeSampleCount(value: unknown) {
  const numeric = Number(value);
  return Math.min(5, Math.max(1, Math.round(numeric) || DEFAULT_SAMPLE_COUNT));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
