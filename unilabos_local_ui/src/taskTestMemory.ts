export type TaskNodeParameters = Record<string, Record<string, unknown>>;
export type TaskParameterSchema = Record<string, Record<string, readonly string[]>>;
export type SampleTemplateParameters = Record<string, Record<string, TaskNodeParameters>>;

export type TaskTestMemory = {
  version: 2;
  sampleCount: number;
  sampleTemplateParameters: SampleTemplateParameters;
};

type StorageReader = Pick<Storage, 'getItem'>;
type StorageWriter = Pick<Storage, 'setItem'>;

const TASK_TEST_MEMORY_PREFIX = 'unilabos.taskTestMemory.v2';
const LEGACY_TASK_TEST_MEMORY_PREFIX = 'unilabos.taskTestMemory.v1';
const DEFAULT_SAMPLE_COUNT = 3;

export function generateSampleIds(
  sampleCount: number,
  existingSampleIds: readonly string[] = [],
): string[] {
  const startOrdinal = existingSampleIds.reduce((maximum, sampleId) => {
    const match = /^Sample\s+([A-Z]+)$/i.exec(sampleId.trim());
    if (!match) return maximum;
    const ordinal = [...match[1].toUpperCase()].reduce(
      (value, character) => value * 26 + character.charCodeAt(0) - 64,
      0,
    );
    return Math.max(maximum, ordinal);
  }, 0) + 1;
  return Array.from({ length: normalizeSampleCount(sampleCount) }, (_, index) => {
    let value = startOrdinal + index;
    let suffix = '';
    while (value > 0) {
      value -= 1;
      suffix = String.fromCharCode(65 + (value % 26)) + suffix;
      value = Math.floor(value / 26);
    }
    return `Sample ${suffix}`;
  });
}

export function createEmptyTaskTestMemory(): TaskTestMemory {
  return {
    version: 2,
    sampleCount: DEFAULT_SAMPLE_COUNT,
    sampleTemplateParameters: {},
  };
}

export function taskTestMemoryKey(workflowPath: string) {
  return `${TASK_TEST_MEMORY_PREFIX}.${workflowPath || 'default'}`;
}

export function loadTaskTestMemory(storage: StorageReader, workflowPath: string): TaskTestMemory {
  try {
    const suffix = workflowPath || 'default';
    const raw = storage.getItem(taskTestMemoryKey(workflowPath))
      || storage.getItem(`${LEGACY_TASK_TEST_MEMORY_PREFIX}.${suffix}`);
    if (!raw) return createEmptyTaskTestMemory();
    const parsed = JSON.parse(raw) as unknown;
    if (!isRecord(parsed)) {
      return createEmptyTaskTestMemory();
    }
    if (parsed.version === 1) {
      // v1 没有保存 sample_id，无法可靠判断最后一份参数属于哪个样品；只迁移样品数，
      // 避免把最后编辑的 Sample D 参数错误套用到 Sample A/B/C。
      return {
        ...createEmptyTaskTestMemory(),
        sampleCount: normalizeSampleCount(parsed.sampleCount),
      };
    }
    if (parsed.version !== 2 || !isRecord(parsed.sampleTemplateParameters)) {
      return createEmptyTaskTestMemory();
    }
    const sampleTemplateParameters = normalizeSampleTemplateParameters(
      parsed.sampleTemplateParameters,
    );
    return {
      version: 2,
      sampleCount: normalizeSampleCount(parsed.sampleCount),
      sampleTemplateParameters,
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

export function withRememberedSampleTemplateParameters(
  memory: TaskTestMemory,
  sampleId: string,
  templateId: string,
  nodeParameters: TaskNodeParameters,
): TaskTestMemory {
  if (!sampleId || !templateId) return memory;
  if (!Object.keys(nodeParameters).length) {
    const { [templateId]: _removed, ...remainingTemplates } = (
      memory.sampleTemplateParameters[sampleId] || {}
    );
    const { [sampleId]: _sample, ...remainingSamples } = memory.sampleTemplateParameters;
    return {
      ...memory,
      sampleTemplateParameters: Object.keys(remainingTemplates).length
        ? { ...memory.sampleTemplateParameters, [sampleId]: remainingTemplates }
        : remainingSamples,
    };
  }
  return {
    ...memory,
    sampleTemplateParameters: {
      ...memory.sampleTemplateParameters,
      [sampleId]: {
        ...(memory.sampleTemplateParameters[sampleId] || {}),
        [templateId]: cloneNodeParameters(nodeParameters),
      },
    },
  };
}

export function withoutRememberedTemplateParameters(memory: TaskTestMemory): TaskTestMemory {
  return { ...memory, sampleTemplateParameters: {} };
}

export function rememberedParametersForSamples(
  memory: TaskTestMemory,
  sampleIds: string[],
  templateIds: string[],
  schema?: TaskParameterSchema,
): SampleTemplateParameters {
  const selectedSamples = new Set(sampleIds);
  const selectedTemplates = new Set(templateIds);
  return Object.fromEntries(
    Object.entries(memory.sampleTemplateParameters)
      .filter(([sampleId]) => selectedSamples.has(sampleId))
      .flatMap(([sampleId, templateParameters]) => {
        const filteredTemplates = Object.fromEntries(
          Object.entries(templateParameters)
            .filter(([templateId]) => selectedTemplates.has(templateId))
            .flatMap(([templateId, parameters]) => {
              const filtered = schema
                ? filterNodeParameters(parameters, schema[templateId] || {})
                : cloneNodeParameters(parameters);
              return Object.keys(filtered).length ? [[templateId, filtered]] : [];
            }),
        );
        return Object.keys(filteredTemplates).length ? [[sampleId, filteredTemplates]] : [];
      }),
  );
}

export function rememberedSampleTemplateCount(
  memory: TaskTestMemory,
  templateIds?: string[],
) {
  const selectedTemplates = templateIds ? new Set(templateIds) : null;
  return Object.values(memory.sampleTemplateParameters).reduce(
    (count, templates) => count + Object.keys(templates).filter(
      (templateId) => !selectedTemplates || selectedTemplates.has(templateId),
    ).length,
    0,
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

function normalizeSampleTemplateParameters(value: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(value).flatMap(([sampleId, templates]) => {
      if (!sampleId || !isRecord(templates)) return [];
      const normalizedTemplates = Object.fromEntries(
        Object.entries(templates).flatMap(([templateId, parameters]) => {
          const normalized = normalizeNodeParameters(parameters);
          return templateId && normalized ? [[templateId, normalized]] : [];
        }),
      );
      return Object.keys(normalizedTemplates).length ? [[sampleId, normalizedTemplates]] : [];
    }),
  );
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
  return Math.min(999, Math.max(1, Math.round(numeric) || DEFAULT_SAMPLE_COUNT));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
