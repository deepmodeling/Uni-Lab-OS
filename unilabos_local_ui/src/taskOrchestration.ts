export type CsvVariableModel = {
  name: string;
  data_type: string;
  initial_value: string;
};

export type TriggerCondition = {
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

export type TaskInstanceModel = {
  id: string;
  sample: string;
  templateId: string;
  order: number;
  status: 'waiting' | 'pending' | 'running' | 'done';
  startedAt?: number;
  finishedAt?: number;
};

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
};

function templateDurationMs(template: TaskTemplateModel) {
  return Math.max(15_000, template.nodeIds.length * 15_000);
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

function normalizedDataType(dataType: string) {
  return dataType.trim().toUpperCase();
}

export function createDefaultTriggerCondition(variable?: CsvVariableModel): TriggerCondition {
  const dataType = normalizedDataType(variable?.data_type || 'STRING');
  const initialValue = variable?.initial_value ?? '';
  if (dataType === 'BOOL' || dataType === 'BOOLEAN') {
    return {
      variableName: variable?.name || '',
      dataType,
      value: initialValue.trim().toLowerCase() === 'true',
    };
  }
  if (dataType === 'INTEGER' || dataType === 'INT' || dataType === 'FLOAT' || dataType === 'DOUBLE' || dataType === 'NUMBER') {
    const numericValue = Number(initialValue);
    return {
      variableName: variable?.name || '',
      dataType,
      value: Number.isFinite(numericValue) ? numericValue : 0,
    };
  }
  return { variableName: variable?.name || '', dataType, value: initialValue };
}

/**
 * 将自动识别的运行约束转成可审阅的 Task 输入/输出条件。
 * 资源锁和工位门控由后端调度策略判定，输出只保留完成审计事件。
 */
export function createTaskTemplateTriggers(resources: string[], gates: string[]) {
  const unique = (items: string[]) => Array.from(new Set(items.filter(Boolean)));
  const booleanCondition = (variableName: string): TriggerCondition => ({
    variableName,
    dataType: 'BOOL',
    value: true,
  });

  return {
    inputTriggers: [
      ...unique(resources).map((resource) => booleanCondition(`系统资源可用：${resource}`)),
      ...unique(gates).map((gate) => booleanCondition(`系统工位可用：${gate}`)),
    ],
    outputTriggers: [
      ...unique(resources).map((resource) => booleanCondition(`系统资源释放：${resource}`)),
      ...unique(gates).map((gate) => booleanCondition(`系统工位完成：${gate}`)),
    ],
  };
}

function isDerivedTaskTrigger(condition: TriggerCondition) {
  return /^(系统资源可用|系统工位可用|系统资源释放|系统工位完成|资源锁可获取|工位条件满足|资源锁释放|工位任务完成|业务内部)：/.test(condition.variableName);
}

export function normalizeTriggerConditions(
  conditions: TriggerCondition[],
  csvVariables: CsvVariableModel[],
): TriggerCondition[] {
  if (!csvVariables.length) {
    return conditions.length ? conditions : [createDefaultTriggerCondition()];
  }
  const variablesByName = new Map(csvVariables.map((variable) => [variable.name, variable]));
  const normalized = conditions.flatMap((condition) => {
    if (isDerivedTaskTrigger(condition)) return [condition];
    const variable = variablesByName.get(condition.variableName);
    if (!variable) return [];
    return [{
      variableName: variable.name,
      dataType: normalizedDataType(variable.data_type),
      value: condition.value,
    }];
  });
  return normalized.length ? normalized : [createDefaultTriggerCondition(csvVariables[0])];
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

export function buildTaskGanttSchedule(
  templates: TaskTemplateModel[],
  instances: TaskInstanceModel[],
  now: number,
): TaskGanttEntry[] {
  const templatesById = new Map(templates.map((template) => [template.id, template]));
  const resourceAvailableAt = new Map<string, number>();
  const sampleAvailableAt = new Map<string, number>();
  const entries: TaskGanttEntry[] = [];

  instances.forEach((instance) => {
    const template = templatesById.get(instance.templateId);
    if (!template) return;
    const duration = templateDurationMs(template);
    const previousSampleEnd = sampleAvailableAt.get(instance.sample) ?? 0;
    const resourceReadyAt = template.resources.reduce(
      (latest, resource) => Math.max(latest, resourceAvailableAt.get(resource) ?? 0),
      0,
    );
    const plannedStart = Math.max(now, previousSampleEnd, resourceReadyAt);
    const state = instance.status === 'done'
      ? 'done'
      : instance.status === 'running'
        ? 'running'
        : 'planned';
    const endAt = instance.finishedAt ?? (instance.status === 'running' ? now + duration : plannedStart + duration);
    const startAt = instance.startedAt ?? (state === 'done' ? Math.max(0, endAt - duration) : plannedStart);
    const availableAt = Math.max(endAt, startAt);

    template.resources.forEach((resource) => {
      resourceAvailableAt.set(resource, availableAt);
      entries.push({
        id: `${instance.id}:${resource}`,
        instanceId: instance.id,
        sample: instance.sample,
        templateId: template.id,
        templateName: template.name,
        resource,
        startAt,
        endAt,
        state,
      });
    });
    sampleAvailableAt.set(instance.sample, availableAt);
  });

  return entries;
}
