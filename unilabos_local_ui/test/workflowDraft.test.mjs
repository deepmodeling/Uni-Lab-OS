import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

async function importTypeScriptModule(path) {
  const source = await readFile(path, 'utf8');
  return importTypeScriptSource(source);
}

async function importTypeScriptSource(source) {
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2020,
      strict: true,
    },
  });
  const tempDir = await mkdtemp(join(tmpdir(), 'workflow-draft-test-'));
  const tempFile = join(tempDir, 'workflowDraft.mjs');
  await writeFile(tempFile, transpiled.outputText, 'utf8');
  return import(tempFile);
}

const {
  createExecutionEdgeOverlay,
  createExecutionPlan,
  createImportedDraft,
  createWorkflowRequest,
  layoutFlowGraph,
  workflowDraftKey,
} = await importTypeScriptModule(
  new URL('../src/workflowDraft.ts', import.meta.url),
);
const { collectOpcChanges, formatOpcValue } = await importTypeScriptModule(
  new URL('../src/opcChanges.ts', import.meta.url),
);
const { formatUiError, buildWorkspaceSummary, groupActionsByDevice } = await importTypeScriptModule(
  new URL('../src/uiState.ts', import.meta.url),
);
const {
  buildTaskGanttSchedule,
  createTaskTemplateTriggers,
  createDefaultTriggerCondition,
  normalizeTriggerConditions,
  renameTaskTemplate,
  updateTaskTemplateTriggers,
} = await importTypeScriptModule(
  new URL('../src/taskOrchestration.ts', import.meta.url),
);
const {
  createTaskOrchestrationClient,
  resolveTaskOrchestrationApiUrl,
  resolveTaskOrchestrationUiToken,
  TaskOrchestrationBusinessError,
  TaskOrchestrationServiceUnavailableError,
  toApiTrigger,
} = await importTypeScriptModule(
  new URL('../src/taskOrchestrationApi.ts', import.meta.url),
);
const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
const styleSource = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
const opcChangesSource = await readFile(new URL('../src/opcChanges.ts', import.meta.url), 'utf8');
const taskStateSource = mainSource.match(
  /export function createEmptyTaskWorkspaceState\([\s\S]*?\n}\n\ntype StackSlotPayload/,
)?.[0].replace(/\n\ntype StackSlotPayload$/, '') || '';
const {
  createEmptyTaskWorkspaceState,
  clampContextMenuPosition,
  isRestorableContextMenuFocusTarget,
  removeTaskTemplateState,
  resetTaskWorkspaceState,
} = await importTypeScriptSource(taskStateSource);

assert.equal(
  resolveTaskOrchestrationApiUrl({ VITE_TASK_ORCHESTRATION_API_URL: 'http://scheduler.test/api/v1/' }),
  'http://scheduler.test/api/v1',
  '任务编排 API 地址应使用环境变量并规范化末尾斜杠',
);
assert.equal(
  resolveTaskOrchestrationApiUrl({}),
  '/task-api/api/v1',
  '未配置环境变量时应使用 Vite 任务编排代理地址',
);
assert.equal(
  resolveTaskOrchestrationApiUrl({}, { protocol: 'http:', hostname: '127.0.0.1', port: '8014' }),
  'http://127.0.0.1:8091/api/v1',
  '由 workflow_ui 托管时应直连排程服务，避免落入前端静态回退路由',
);
assert.equal(
  resolveTaskOrchestrationUiToken({}, { hostname: '127.0.0.1', port: '8014' }),
  'local-task-ui-dev',
  '本地 workflow_ui 应使用与排程服务一致的开发 UI 令牌',
);
assert.deepEqual(
  toApiTrigger({ variableName: 'S09 空闲', dataType: 'BOOL', value: true }),
  { kind: 'opc', config: { provider_id: 'default', variable: 'S09 空闲', value: true } },
  'CSV OPC 条件应映射为后端可判定的默认 provider DTO',
);
assert.deepEqual(
  toApiTrigger({ variableName: '系统资源可用：robot', dataType: 'BOOL', value: true }),
  { kind: 'resource', config: { resource: 'robot' } },
  '派生资源约束应映射为后端 resource Trigger DTO',
);
assert.deepEqual(
  toApiTrigger({ variableName: '系统资源释放：robot', dataType: 'BOOL', value: true }),
  { kind: 'resource', config: { resource: 'robot', event: 'released' } },
  '派生资源输出应记录审计事件而不形成内部等待条件',
);
const taskApiRequests = [];
const taskApi = createTaskOrchestrationClient({
  baseUrl: 'http://scheduler.test/api/v1',
  fetchImpl: async (url, init) => {
    taskApiRequests.push({ url, init });
    return new Response(JSON.stringify({
      version: 3,
      workspace: {
        workflow_path: '/tmp/demo.json',
        templates: [],
        task_instances: [],
        events: [],
        scheduled_template_ids: [],
        scheduler_paused: false,
        schedule_entries: [],
        opc_snapshots: [{ provider_id: 'gateway', sequence: 2, variable_count: 4, updated_at_by_variable: {} }],
      },
    }), { status: 200, headers: { 'content-type': 'application/json' } });
  },
});
await taskApi.getWorkspace('/tmp/demo.json');
assert.equal(
  taskApiRequests[0].url,
  'http://scheduler.test/api/v1/workspaces?workflow_path=%2Ftmp%2Fdemo.json',
  '读取工作区必须携带 workflow_path 查询参数',
);
assert.equal(
  taskApiRequests[0].init.method,
  'GET',
  '读取工作区必须通过 GET 请求',
);
await taskApi.updateScheduledTemplates('/tmp/demo.json', 3, ['second', 'first']);
assert.equal(
  taskApiRequests[1].url,
  'http://scheduler.test/api/v1/workspaces/scheduled-templates',
  '待排模板顺序必须通过专用 API 持久化',
);
assert.deepEqual(
  JSON.parse(taskApiRequests[1].init.body),
  { workflow_path: '/tmp/demo.json', expected_version: 3, template_ids: ['second', 'first'] },
  '待排模板请求只能包含工作区、版本和模板 ID',
);
await assert.rejects(
  () => createTaskOrchestrationClient({
    baseUrl: 'http://scheduler.test/api/v1',
    fetchImpl: async () => new Response(JSON.stringify({ detail: { message: '模板不存在' } }), { status: 404 }),
  }).getWorkspace('/tmp/demo.json'),
  (error) => error instanceof TaskOrchestrationBusinessError && error.message === '模板不存在',
  '404/409/422 应保留为可展示的业务错误',
);
await assert.rejects(
  () => createTaskOrchestrationClient({
    baseUrl: 'http://scheduler.test/api/v1',
    fetchImpl: async () => new Response('', { status: 503 }),
  }).getWorkspace('/tmp/demo.json'),
  (error) => error instanceof TaskOrchestrationServiceUnavailableError,
  '5xx 应归类为服务不可用错误',
);
await assert.rejects(
  () => createTaskOrchestrationClient({
    baseUrl: 'http://scheduler.test/api/v1',
    fetchImpl: async () => new Response(JSON.stringify({ version: 3, workspace: {} }), { status: 200 }),
  }).getWorkspace('/tmp/demo.json'),
  (error) => error instanceof TaskOrchestrationServiceUnavailableError,
  '无效成功响应必须归类为服务协议错误',
);

const taskTemplatesFixture = [
  { id: 'solid', name: 'S07 固体加料', nodeIds: ['n1', 'n2'], resources: ['robot', 's07'], gates: ['s07'] },
  { id: 'liquid', name: 'S09 配液', nodeIds: ['n3', 'n4', 'n5'], resources: ['robot', 's09'], gates: ['s09'] },
];
const taskInstancesFixture = [
  { id: 'a-solid', sample: 'Sample A', templateId: 'solid', order: 0, status: 'done', finishedAt: 60_000 },
  { id: 'a-liquid', sample: 'Sample A', templateId: 'liquid', order: 1, status: 'pending' },
  { id: 'b-solid', sample: 'Sample B', templateId: 'solid', order: 0, status: 'pending' },
];
const ganttSchedule = buildTaskGanttSchedule(taskTemplatesFixture, taskInstancesFixture, 60_000);
assert.deepEqual(
  ganttSchedule.map((item) => ({ instanceId: item.instanceId, resource: item.resource, state: item.state })),
  [
    { instanceId: 'a-solid', resource: 'robot', state: 'done' },
    { instanceId: 'a-solid', resource: 's07', state: 'done' },
    { instanceId: 'a-liquid', resource: 'robot', state: 'planned' },
    { instanceId: 'a-liquid', resource: 's09', state: 'planned' },
    { instanceId: 'b-solid', resource: 'robot', state: 'planned' },
    { instanceId: 'b-solid', resource: 's07', state: 'planned' },
  ],
  '甘特排程应按模板资源生成泳道条目，并保留实例运行状态',
);
assert.equal(
  ganttSchedule.find((item) => item.instanceId === 'a-liquid' && item.resource === 'robot')?.startAt,
  60_000,
  '同一样品的后继 Task 应从前置完成时间开始排程',
);
assert.deepEqual(
  renameTaskTemplate(taskTemplatesFixture, 'liquid', 'S09 精准配液').map((template) => template.name),
  ['S07 固体加料', 'S09 精准配液'],
  '重命名模板应只更新指定模板',
);
assert.deepEqual(
  updateTaskTemplateTriggers(
    [{
      ...taskTemplatesFixture[0],
      inputTriggers: [{ variableName: 'S07 空闲', dataType: 'BOOL', value: false }],
      outputTriggers: [{ variableName: '加料完成', dataType: 'BOOL', value: false }],
    }],
    'solid',
    'output',
    [{ variableName: 'S07 工位释放', dataType: 'BOOL', value: true }],
  )[0].outputTriggers,
  [{ variableName: 'S07 工位释放', dataType: 'BOOL', value: true }],
  '模板应支持独立更新输出触发列表',
);
const csvVariablesFixture = [
  { name: 'ready', data_type: 'BOOL', initial_value: 'true' },
  { name: 'batch', data_type: 'INTEGER', initial_value: '12' },
  { name: 'temperature', data_type: 'FLOAT', initial_value: '23.5' },
  { name: 'label', data_type: 'STRING', initial_value: '样品 A' },
];
assert.deepEqual(
  createTaskTemplateTriggers(['robot', 's07'], ['s07']),
  {
    inputTriggers: [
      { variableName: '系统资源可用：robot', dataType: 'BOOL', value: true },
      { variableName: '系统资源可用：s07', dataType: 'BOOL', value: true },
      { variableName: '系统工位可用：s07', dataType: 'BOOL', value: true },
    ],
    outputTriggers: [
      { variableName: '系统资源释放：robot', dataType: 'BOOL', value: true },
      { variableName: '系统资源释放：s07', dataType: 'BOOL', value: true },
      { variableName: '系统工位完成：s07', dataType: 'BOOL', value: true },
    ],
  },
  '模板应将识别出的资源锁与工位门控转换为详情中的输入和输出条件',
);
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[0]),
  { variableName: 'ready', dataType: 'BOOL', value: true },
  '布尔 CSV 变量应以初始 true 值创建条件',
);
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[1]),
  { variableName: 'batch', dataType: 'INTEGER', value: 12 },
  '整数 CSV 变量应以数字初始值创建条件',
);
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[2]),
  { variableName: 'temperature', dataType: 'FLOAT', value: 23.5 },
  '浮点 CSV 变量应以数字初始值创建条件',
);
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[3]),
  { variableName: 'label', dataType: 'STRING', value: '样品 A' },
  '字符串 CSV 变量应以文本初始值创建条件',
);
assert.deepEqual(
  normalizeTriggerConditions([], csvVariablesFixture),
  [{ variableName: 'ready', dataType: 'BOOL', value: true }],
  '输入或输出条件为空时必须补充至少一条默认条件',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [{ variableName: 'missing', dataType: 'STRING', value: '旧值' }],
    csvVariablesFixture,
  ),
  [{ variableName: 'ready', dataType: 'BOOL', value: true }],
  '已不存在于 CSV 的变量应替换为默认 CSV 条件',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [{ variableName: '业务内部：样品已制备', dataType: 'BOOL', value: true }],
    csvVariablesFixture,
  ),
  [{ variableName: '业务内部：样品已制备', dataType: 'BOOL', value: true }],
  '业务内部链路条件必须保留稳定 key，而不是被 CSV 过滤',
);
assert.match(
  mainSource,
  /const \[isSchedulerRunning, setIsSchedulerRunning\] = useState\(false\);/,
  'Task 编排应维护运行或暂停状态',
);
assert.match(
  mainSource,
  /isSchedulerRunning \? '暂停派发' : '运行调度'/,
  'Task 编排应提供运行和暂停派发按钮',
);
assert.match(
  mainSource,
  /value=\{selectedTaskTemplate\?\.name \|\| ''\}[\s\S]*?onBlur=\{\(event\) => renameSelectedTaskTemplate\(event\.target\.value\)\}/,
  '选中模板详情应在失焦时通过 API 保存重命名',
);
assert.match(mainSource, /createTaskOrchestrationClient\(\)/, 'Task 工作区应使用独立 REST API 客户端');
assert.match(mainSource, /Task 编排服务不可用[\s\S]*?重试/, '服务不可用时应显示明确状态和重试按钮');
assert.match(mainSource, /taskApiRef\.current\.updateScheduledTemplates/, '待排模板变更必须通过专用 API');
assert.match(mainSource, /taskApiRef\.current\.advance/, '调度推进必须通过 API');
assert.match(mainSource, /satisfied_triggers/, '调度日志应展示启动时已经满足的输入条件');
assert.doesNotMatch(mainSource, /scheduleOneTask/, '前端不得保留本地定时完成模拟');
assert.match(mainSource, /taskRequestQueueRef\.current\.then\(execute, execute\)/, '所有 Task 写操作应串行进入单一请求队列');
assert.match(mainSource, /error instanceof TaskOrchestrationBusinessError && error\.status === 409[\s\S]*?getWorkspace\(taskWorkspacePath\)[\s\S]*?operation\(latest\.version\)/, '409 应 reload 最新 workspace 后仅重试一次');
assert.match(mainSource, /taskPollingTimerRef[\s\S]*?taskPollingInFlightRef[\s\S]*?taskPollingGenerationRef/, '轮询应维护 timer、inflight 与 generation ref');
assert.match(
  mainSource,
  /schedule_entries\.flatMap\(\(entry\) => entry\.resources\.map\(\(resource\)/,
  'API 工位资源必须映射为 Resource Schedule 泳道',
);
assert.match(
  mainSource,
  /error instanceof TaskOrchestrationServiceUnavailableError[\s\S]*?return error instanceof Error \? error\.message/,
  'Task API 业务错误应保留服务 detail，不应伪装为服务不可用',
);
assert.match(
  mainSource,
  /taskServiceError === 'Task 编排服务不可用' && \(\s*<button[\s\S]*?重试/,
  '仅服务不可用状态应显示重试按钮',
);
assert.match(
  mainSource,
  /<section className="task-column task-gantt-column">[\s\S]*?taskGanttEntries/,
  'Task 编排应渲染资源泳道甘特图',
);
assert.match(
  mainSource,
  /const moveTaskInstance = useCallback\(\(taskId: string, direction: -1 \| 1\)/,
  'Queue 应支持手动调整同一样品内的 Task 执行顺序',
);
assert.match(
  mainSource,
  /aria-label=\{`上移 \$\{task\.sample\}\/\$\{template\?\.name \|\| task\.templateId\}`\}[\s\S]*?moveTaskInstance\(task\.id, -1\)/,
  'Queue 卡片应提供上移执行顺序按钮',
);
assert.match(
  styleSource,
  /\.task-template-list\s*\{[\s\S]*?flex:\s*1;[\s\S]*?min-height:\s*0;[\s\S]*?overflow:\s*auto;/,
  '三列布局中 Template 列表应填满自身列并独立滚动',
);
assert.match(
  mainSource,
  /<h2>Sensor Gates<\/h2>/,
  'Task 编排主画面应恢复 Sensor Gates 面板',
);
assert.match(
  mainSource,
  /<h2>等待条件与调度事件<\/h2>[\s\S]*?taskEvents\.map/,
  '调度区域应展示运行中实际等待的信号或资源原因',
);
assert.match(
  mainSource,
  /const \[scheduledTemplateIds, setScheduledTemplateIds\] = useState<string\[\]>\(\[\]\);/,
  'Task 编排应维护手动加入 Resource Schedule 的模板序列',
);
assert.match(
  mainSource,
  /const addTemplateToSchedule = useCallback\(\(templateId: string\) =>/,
  'Template 应可被手动加入 Resource Schedule',
);
assert.match(
  mainSource,
  /draggable=\{true\}[\s\S]*?onDragStart=\{\(event\) => event\.dataTransfer\.setData\('application\/x-unilab-task-template', template\.id\)\}/,
  '模板卡片应支持拖入 Resource Schedule',
);
assert.match(
  mainSource,
  /onDrop=\{\(event\) => \{[\s\S]*?application\/x-unilab-task-template[\s\S]*?addTemplateToSchedule\(templateId\);/,
  'Resource Schedule 应接收拖入的模板',
);
assert.match(
  styleSource,
  /\.task-orchestration\s*\{[\s\S]*?grid-template-columns:\s*minmax\([^;]+\)\s+minmax\([^;]+\)\s+minmax\([^;]+\);/,
  'Task 编排应使用 Template、详情、Queue 三列布局',
);
assert.match(
  mainSource,
  /scheduledTemplateIds\.length\s*\?\s*taskGanttEntries\.map\(\(entry\) => entry\.resource\)/,
  'Resource Schedule 应从 API 甘特条目的资源生成泳道，并在待排为空时隐藏',
);
assert.match(
  mainSource,
  /scheduledResources\.map\(\(resource\) =>/,
  'Resource Schedule 不应固定渲染全部默认工位',
);
assert.match(
  mainSource,
  /本次待排 \{scheduledTemplateIds\.length\} \/ \{taskTemplates\.length\}/,
  '待排区域应明确区分手动拖入数量与全部模板数量',
);
assert.match(
  mainSource,
  /const updateSelectedTaskTriggers = useCallback\(\(kind: 'input' \| 'output', triggers: TriggerCondition\[\]\) =>/,
  'Template Detail 应提供类型化的输入与输出触发条件编辑',
);
assert.match(
  mainSource,
  /placeholder="搜索全部 OPC 变量"[\s\S]*?添加输入条件[\s\S]*?添加输出条件/,
  '输入和输出条件均应从可搜索的全量 OPC 变量菜单新增',
);
assert.match(
  mainSource,
  /placeholder="搜索全部 OPC 变量"[\s\S]*?className="task-trigger-options"/,
  '触发条件编辑器应提供可见的全量 OPC 变量搜索结果',
);
assert.match(
  styleSource,
  /\.task-trigger-options\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?z-index:\s*20;/,
  'OPC 变量搜索结果应作为置顶浮层展开',
);
assert.match(
  styleSource,
  /\.task-trigger-condition \.task-trigger-options button\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(72px,\s*0\.4fr\)\s+auto;/,
  'OPC 变量候选项应保持单行布局，避免被删除按钮样式覆盖',
);
assert.match(
  styleSource,
  /\.task-trigger-condition \.task-trigger-options button\s*\{[\s\S]*?font-weight:\s*500;[\s\S]*?font-size:\s*12px;/,
  'OPC 变量候选项应使用紧凑的常规字重，而非调试面板式粗体',
);
assert.match(
  styleSource,
  /\.task-trigger-condition \.task-trigger-options button\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(72px,\s*0\.4fr\)\s+auto;[\s\S]*?text-align:\s*left;/,
  'OPC 变量候选项应将名称和类型固定为左对齐的两列',
);
assert.match(
  styleSource,
  /\.task-trigger-options\s*\{[\s\S]*?overflow-x:\s*hidden;[\s\S]*?overscroll-behavior:\s*contain;/,
  'OPC 变量列表应禁止横向偏移并隔离自身滚动',
);
assert.match(
  styleSource,
  /\.task-trigger-options b\s*\{[\s\S]*?justify-self:\s*stretch;[\s\S]*?text-align:\s*left;/,
  'OPC 变量名称应锚定在候选项左侧',
);
assert.match(
  mainSource,
  /setTriggerSearchQueries\(\(current\) => \(\{[\s\S]*?\[`input-\$\{index\}`\]: '',/,
  '聚焦输入条件变量时应清空筛选词，以展示完整 OPC 变量目录',
);
assert.match(
  mainSource,
  /trigger\.dataType === 'BOOL'[\s\S]*?<select[\s\S]*?type="number"[\s\S]*?createDefaultTriggerCondition\(variable\)/,
  '条件值编辑器应随 BOOL、数值和字符串 CSV 类型切换，并在选变量时采用初始值',
);
assert.doesNotMatch(
  mainSource,
  /newInputTrigger|newOutputTrigger|placeholder="例如：S09 工位空闲"/,
  '触发条件不应保留自由文本编辑入口',
);
assert.match(
  mainSource,
  /const \[resourceScheduleHeight, setResourceScheduleHeight\] = useState\(260\);/,
  'Resource Schedule 应维护可调整高度',
);
assert.match(
  mainSource,
  /const startResourceScheduleResize = useCallback\(\(event: React\.PointerEvent<HTMLDivElement>\) =>/,
  'Resource Schedule 应支持指针拖拽调整高度',
);
assert.match(
  mainSource,
  /className="task-schedule-resize-handle"[\s\S]*?onPointerDown=\{startResourceScheduleResize\}/,
  'Resource Schedule 顶部应渲染拖拽分隔条',
);
assert.doesNotMatch(
  mainSource,
  /<div className="task-mini-tags">/,
  'Template 卡片不应将资源锁或门控直接展示为业务触发',
);
assert.match(
  styleSource,
  /\.task-schedule-resize-handle\s*\{[\s\S]*?cursor:\s*row-resize;/,
  'Resource Schedule 分隔条应使用垂直调整光标',
);

const toolbarSource = mainSource.match(/<div className="demo-canvas-toolbar">[\s\S]*?<div className="demo-tabbar"/)?.[0] || '';
assert.equal(
  /onClick=\{runWorkflow\}/.test(toolbarSource),
  false,
  '顶部画布工具栏不应包含运行按钮',
);

const runButtonsSource = mainSource.match(/<div className="demo-run-buttons">[\s\S]*?<\/div>/)?.[0] || '';
assert.equal(
  /校验流程/.test(runButtonsSource),
  false,
  '右侧运行按钮区不应重复展示校验流程',
);
assert.match(
  styleSource,
  /\.demo-run-buttons\s*\{[\s\S]*?gap:\s*(1[2-9]|[2-9]\d)px;/,
  '右侧运行按钮区需要至少 12px 间距，避免按钮挤在一起',
);
assert.match(
  styleSource,
  /\.demo-execution-summary\s*\{[^}]*margin-top:\s*(1[0-9]|[2-9]\d)px;/,
  '运行按钮区和执行摘要之间需要至少 10px 间距',
);
assert.match(
  styleSource,
  /\.demo-tool-shell\s*\{[\s\S]*?display:\s*flex;[\s\S]*?flex-direction:\s*column;[\s\S]*?min-height:\s*0;/,
  '工具壳应使用弹性列布局，避免固定最小高度裁切画布',
);
assert.match(
  styleSource,
  /\.demo-workbench\s*\{[\s\S]*?flex:\s*1;[\s\S]*?min-height:\s*0;/,
  '工作台应占用页头和导航之外的剩余高度',
);
assert.doesNotMatch(
  styleSource,
  /\.demo-workbench\s*\{[\s\S]*?height:\s*calc\(100vh - 66px\);/,
  '工作台不能再按未包含页头与导航的固定视口高度计算',
);
assert.match(
  mainSource,
  /<details className="opc-collapsible" open>[\s\S]*?<summary className="opc-changes-head">[\s\S]*?OPC 采样变量/,
  'OPC 采样变量应放在可折叠区域内',
);
assert.match(
  mainSource,
  /<details className="opc-collapsible" open>[\s\S]*?<summary className="opc-changes-head">[\s\S]*?OPC 变量变化/,
  'OPC 变量变化应放在可折叠区域内',
);
assert.match(
  mainSource,
  /leftPanelCollapsed/,
  '左侧联调入口面板应有折叠状态',
);
assert.match(
  mainSource,
  /aria-label=\{leftPanelCollapsed \? '展开联调入口' : '收起联调入口'\}/,
  '左侧联调入口面板应提供可访问的折叠按钮',
);
assert.match(
  styleSource,
  /\.demo-workbench\.left-collapsed\s*\{[^}]*grid-template-columns:\s*64px minmax\(600px, 1fr\) 340px;/,
  '左侧联调入口收起后应变为窄栏',
);
assert.match(
  opcChangesSource,
  /category\?: string/,
  '结构化日志事件应包含 category 标签',
);
assert.match(
  mainSource,
  /selectedLogCategory/,
  '日志面板应支持按 category 筛选',
);
assert.match(
  mainSource,
  /className="log-category-tabs"/,
  '日志面板应渲染分类标签栏',
);
assert.match(
  mainSource,
  /onToggleBypassed/,
  'ActionNode 应接收直通切换回调',
);
const actionNodeSource = mainSource.match(
  /function ActionNode\([\s\S]*?\nfunction executionStateText/,
)?.[0] || '';
const bypassButtonSource = actionNodeSource.match(
  /<button(?:(?!<button)[\s\S])*?<path d="M4 12h15M14 7l5 5-5 5" \/>[\s\S]*?<\/button>/,
)?.[0] || '';
assert.match(
  bypassButtonSource,
  /aria-label=\{data\.executionBypassed \? '取消直通' : '直通此节点'\}/,
  '直通按钮 aria-label 应表达当前可执行动作',
);
assert.match(
  bypassButtonSource,
  /aria-pressed=\{Boolean\(data\.executionBypassed\)\}/,
  '直通按钮应暴露 aria-pressed 状态',
);
assert.match(
  bypassButtonSource,
  /title=\{data\.executionBypassed \? '取消直通' : '直通此节点'\}/,
  '直通按钮标题应反映当前切换状态',
);
assert.match(
  mainSource,
  /executionBypassed:\s*!node\.data\.executionBypassed[\s\S]*?executionDisabled:\s*false/,
  '切换直通时应无条件清除禁用状态',
);
assert.match(
  mainSource,
  /executionDisabled:\s*!node\.data\.executionDisabled[\s\S]*?executionBypassed:\s*false/,
  '切换禁用时应无条件清除直通状态',
);
assert.match(
  styleSource,
  /\.flow-node\.execution-bypassed\s*\{/,
  '直通节点应提供独立视觉样式',
);
assert.match(
  styleSource,
  /\.execution-badge\.bypassed\s*\{/,
  '直通徽标应提供独立视觉样式',
);
assert.match(
  styleSource,
  /\.node-hover-actions button\.active\s*\{/,
  '直通按钮激活时应有明显样式',
);
assert.match(
  styleSource,
  /\.flow-node:focus-within \.node-hover-actions/,
  '节点工具栏应在内部按钮获得键盘焦点时显示',
);
assert.match(
  styleSource,
  /\.node-hover-actions button:focus-visible\s*\{[^}]*outline:/,
  '节点工具栏按钮应提供明确的键盘焦点轮廓',
);
const taskTemplatesSource = mainSource.match(
  /<div className="task-template-list">[\s\S]*?<section className="task-column task-scheduler-column">/,
)?.[0] || '';
const deleteTaskTemplateSource = mainSource.match(
  /const deleteTaskTemplate = useCallback\([\s\S]*?\n  \}, \[mutateTaskWorkspace, showCanvasToast, taskInstances, taskTemplates, taskWorkspacePath\]\);/,
)?.[0] || '';
assert.match(
  deleteTaskTemplateSource,
  /if \(!window\.confirm\([\s\S]*?\)\) \{\s*return;\s*\}/,
  '删除 Task 模板前应通过确认框征求用户确认',
);
assert.match(
  deleteTaskTemplateSource,
  /taskApiRef\.current\.deleteTemplate\(taskWorkspacePath, version, template\.id\)/,
  '确认删除后应通过 API 级联更新模板和实例',
);
assert.match(
  deleteTaskTemplateSource,
  /mutateTaskWorkspace/,
  '删除后应回写 API 工作区响应',
);
assert.match(
  mainSource,
  /const showCanvasToast = useCallback\(\(text: string\) => \{[\s\S]*?\n  \}, \[\]\);/,
  '画布提示函数应使用稳定的 useCallback 引用',
);
assert.ok(
  mainSource.indexOf('const showCanvasToast = useCallback') < mainSource.indexOf('const createTaskTemplateFromNodes = useCallback'),
  '画布提示函数应定义在使用它的 Task 回调之前',
);
assert.match(
  mainSource,
  /const createTaskTemplateFromNodes = useCallback\([\s\S]*?taskApiRef\.current\.createTemplate/,
  '创建 Task 模板回调应写入 API',
);
assert.match(
  mainSource,
  /const createRecommendedTaskTemplates = useCallback\([\s\S]*?taskApiRef\.current\.createTemplate/,
  '自动切分 Task 模板回调应写入 API',
);
assert.match(
  deleteTaskTemplateSource,
  /showCanvasToast\('已删除 Task 模板'\)/,
  '删除 Task 模板回调应保留成功提示',
);
assert.match(
  taskTemplatesSource,
  /className="task-template-delete"[\s\S]*?onClick=\{\(\) => deleteTaskTemplate\(template\)\}[\s\S]*?aria-label=\{`删除 Task 模板 \$\{template\.name\}`\}/,
  'Task Template 卡片应提供动态 aria-label 的删除按钮',
);
assert.match(
  styleSource,
  /\.task-template-delete\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?top:\s*\d+px;[\s\S]*?right:\s*\d+px;/,
  'Task Template 删除按钮应定位在卡片右上角',
);
assert.match(
  styleSource,
  /\.task-template-delete:hover[\s\S]*?\.task-template-delete:focus-visible\s*\{/,
  'Task Template 删除按钮应提供 hover 和键盘焦点样式',
);
assert.match(
  mainSource,
  /type Workspace = 'workflow' \| 'tasks';/,
  'Task 编排应使用独立的一级工作区类型',
);
assert.match(
  mainSource,
  /const \[workspace, setWorkspace\] = useState<Workspace>\('workflow'\);/,
  '应用应默认进入流程设计工作区',
);
assert.match(
  mainSource,
  /<nav className="workspace-navigation" aria-label="一级工作区">/,
  '应用应提供可访问的一级工作区导航',
);
assert.match(
  mainSource,
  /aria-pressed=\{workspace === 'workflow'\}[\s\S]*?流程设计/,
  '流程设计入口应暴露当前选中状态',
);
assert.match(
  mainSource,
  /aria-pressed=\{workspace === 'tasks'\}[\s\S]*?Task 编排/,
  'Task 编排入口应暴露当前选中状态',
);
assert.match(
  mainSource,
  /workspace === 'workflow' && \([\s\S]*?<main className=\{`demo-workbench/,
  '流程设计三栏应仅在流程工作区渲染',
);
assert.match(
  mainSource,
  /workspace === 'tasks' && \(\s*<main className="task-workspace">/,
  'Task 编排应在独立全宽工作区渲染',
);
assert.doesNotMatch(
  mainSource,
  /mainTab === 'tasks'/,
  '旧 mainTab 不应再包含 Task 编排分支',
);
const importFlowSource = mainSource.match(
  /const importFlowJson = async[\s\S]*?\n  };/,
)?.[0] || '';
assert.match(
  importFlowSource,
  /setTaskWorkspacePath\(file\.name\);/,
  '导入新 Flow JSON 后应以导入路径作为 Task 工作区键',
);
const presetLoadSource = mainSource.match(
  /fetch\('\/api\/preset'\)[\s\S]*?\.catch/,
)?.[0] || '';
assert.match(
  presetLoadSource,
  /setTaskWorkspacePath\(`\$\{payload\.default_workflow_name \|\| 'szlab_canvas_workflow'\}\.json`\);/,
  '加载 preset 后应以 workflow 路径作为 Task 工作区键',
);
assert.match(
  styleSource,
  /\.task-workspace\s*\{/,
  'Task 一级工作区应提供独立布局样式',
);
const workflowWorkspaceSource = mainSource.match(
  /\{workspace === 'workflow' && \([\s\S]*?<\/main>\s*\)\}/,
)?.[0] || '';
const taskWorkspaceSource = mainSource.match(
  /\{workspace === 'tasks' && \([\s\S]*?<\/main>\s*\)\}/,
)?.[0] || '';
const canvasTabsSource = workflowWorkspaceSource.match(
  /<div className="demo-tabbar canvas-tabs"[\s\S]*?<\/div>/,
)?.[0] || '';
assert.match(
  mainSource,
  /const \[contextMenu, setContextMenu\] = useState<\{ x: number; y: number \} \| null>\(null\);/,
  '流程画布应维护右键菜单的视窗坐标状态',
);
assert.match(
  mainSource,
  /contextMenuFirstActionRef\.current\?\.focus\(\);/,
  '右键浮层打开后应自动聚焦首个可操作按钮',
);
assert.match(
  mainSource,
  /document\.addEventListener\('keydown', closeContextMenuOnEscape\);[\s\S]*?document\.removeEventListener\('keydown', closeContextMenuOnEscape\);/,
  '右键浮层打开时应注册并清理 Escape 键监听',
);
assert.match(
  mainSource,
  /event\.key !== 'Escape'[\s\S]*?closeCanvasContextMenu\(\);/,
  'Escape 应通过统一关闭函数恢复焦点',
);
assert.match(
  mainSource,
  /document\.activeElement === trigger[\s\S]*?canvasWorkspaceRef\.current\?\.focus\(\);/,
  '统一关闭函数应优先恢复触发元素，否则回退到稳定画布容器',
);
assert.match(
  mainSource,
  /const canvasWorkspaceRef = useRef<HTMLDivElement \| null>\(null\);/,
  '流程画布应维护稳定父容器 ref 以恢复焦点',
);
assert.match(
  mainSource,
  /isRestorableContextMenuFocusTarget\(activeElement, document\.body, document\.documentElement\)/,
  '打开右键对话框时应排除 body 和 documentElement 作为焦点恢复目标',
);
assert.match(
  mainSource,
  /const \[isTaskTemplateEditing, setIsTaskTemplateEditing\] = useState\(false\);/,
  '流程画布应维护默认关闭的 Task 模板编辑状态',
);
assert.match(
  workflowWorkspaceSource,
  /aria-label="切换 Task 模板编辑"[\s\S]*?aria-pressed=\{isTaskTemplateEditing\}[\s\S]*?className=\{isTaskTemplateEditing \? 'active' : ''\}[\s\S]*?title=\{isTaskTemplateEditing \? '退出 Task 模板编辑' : '进入 Task 模板编辑'\}/,
  'Task 模板编辑切换按钮应暴露可访问状态与动态标题',
);
assert.match(
  workflowWorkspaceSource,
  /isTaskTemplateEditing && <div className="task-template-editing-hint">模板编辑中：拖拽框选节点后右键创建模板<\/div>/,
  '仅模板编辑模式应显示操作提示',
);
assert.match(
  workflowWorkspaceSource,
  /onContextMenuCapture=\{openCanvasContextMenu\}/,
  '画布容器应在捕获阶段统一处理右键，覆盖框选区域',
);
assert.match(
  mainSource,
  /const openCanvasContextMenu = useCallback\(\(event: React\.MouseEvent<HTMLElement>\) => \{\s*if \(!isTaskTemplateEditing\) return;\s*event\.preventDefault\(\);[\s\S]*?closest(?:<HTMLElement>)?\('\.react-flow__node'\)[\s\S]*?setContextMenu\(\{ x: position\.left, y: position\.top \}\);/,
  '模板编辑模式应统一阻止原生右键菜单，并保留节点右键选择',
);
assert.match(
  workflowWorkspaceSource,
  /ref=\{canvasWorkspaceRef\}[\s\S]*?tabIndex=\{-1\}/,
  '流程画布稳定父容器应可作为焦点恢复回退目标',
);
assert.match(
  workflowWorkspaceSource,
  /selectionOnDrag=\{isTaskTemplateEditing\}/,
  'React Flow 仅在模板编辑模式启用拖拽框选',
);
assert.match(
  workflowWorkspaceSource,
  /panOnDrag=\{!isTaskTemplateEditing\}/,
  '模板编辑模式应禁用左键拖动画布，确保拖拽用于框选',
);
assert.match(
  workflowWorkspaceSource,
  /onPaneClick=\{\(\) => closeCanvasContextMenu\(\{ restoreFocus: false \}\)\}/,
  '点击画布空白处关闭 Task 右键菜单时不应抢夺焦点',
);
assert.match(
  workflowWorkspaceSource,
  /onNodeClick=\{\(\) => closeCanvasContextMenu\(\{ restoreFocus: false \}\)\}/,
  '左键点击节点时应关闭 Task 右键菜单且不抢夺节点焦点',
);
assert.match(
  mainSource,
  /const closeCanvasContextMenu = useCallback\(\(\{ restoreFocus = true \}: \{ restoreFocus\?: boolean \} = \{\}\) => \{[\s\S]*?setContextMenu\(null\);[\s\S]*?window\.requestAnimationFrame/,
  '应通过统一关闭函数清空菜单并在下一帧恢复焦点',
);
assert.match(
  mainSource,
  /document\.addEventListener\('pointerdown', closeContextMenuOnExternalPointerDown, true\);[\s\S]*?document\.removeEventListener\('pointerdown', closeContextMenuOnExternalPointerDown, true\);/,
  '菜单打开时应注册并清理捕获阶段的外部点击监听',
);
assert.match(
  mainSource,
  /contextMenuRef\.current\?\.contains\(target\)[\s\S]*?contextMenuTriggerRef\.current\?\.contains\(target\)/,
  '外部点击判定应忽略菜单内部和 React Flow 触发区域',
);
assert.match(
  mainSource,
  /const closeContextMenuOnExternalPointerDown[\s\S]*?closeCanvasContextMenu\(\{ restoreFocus: false \}\);/,
  '外部 pointerdown 关闭菜单时不应抢夺用户点击目标的焦点',
);
assert.match(
  workflowWorkspaceSource,
  /isTaskTemplateEditing && contextMenu && \(\s*<div[\s\S]*?className="canvas-context-menu"[\s\S]*?style=\{\{ left: contextMenu\.x, top: contextMenu\.y \}\}/,
  'Task 右键菜单仅在编辑模式且有坐标时显示',
);
assert.match(
  workflowWorkspaceSource,
  /role="dialog"[\s\S]*?aria-label="流程画布操作"/,
  '右键浮层应使用简单的 dialog 语义',
);
assert.match(
  workflowWorkspaceSource,
  /disabled=\{!selectedTaskNodes\.length\}[\s\S]*?createTaskTemplateFromSelection\(\);[\s\S]*?closeCanvasContextMenu\(\);[\s\S]*?设为 Task 模板/,
  '右键菜单应仅在有选中节点时允许创建 Task 模板，并在操作后关闭',
);
const createTaskTemplateFromSelectionSource = mainSource.match(
  /const createTaskTemplateFromSelection = useCallback\([\s\S]*?\n  \}, \[createTaskTemplateFromNodes, selectedTaskNodes\]\);/,
)?.[0] || '';
assert.doesNotMatch(
  createTaskTemplateFromSelectionSource,
  /exitTaskTemplateEditing|setIsTaskTemplateEditing\(false\)/,
  '创建 Task 模板后应保持模板编辑模式开启',
);
assert.match(
  workflowWorkspaceSource,
  /setNodes\(\(current\) => current\.map\(\(node\) => \(\{ \.\.\.node, selected: false \}\)\)\);[\s\S]*?closeCanvasContextMenu\(\);[\s\S]*?取消选择/,
  '右键菜单应清除全部选择并关闭',
);
assert.match(
  mainSource,
  /const exitTaskTemplateEditing = useCallback\(\(\) => \{[\s\S]*?setIsTaskTemplateEditing\(false\);[\s\S]*?setNodes\(\(current\) => current\.map\(\(node\) => \(\{ \.\.\.node, selected: false \}\)\)\);[\s\S]*?closeCanvasContextMenu\(\);/,
  '退出模板编辑模式应关闭菜单并清空节点选择',
);
assert.match(
  mainSource,
  /setWorkspace\('workflow'\);[\s\S]*?exitTaskTemplateEditing\(\);/,
  '切换回流程设计时应退出模板编辑模式',
);
assert.match(
  mainSource,
  /setWorkspace\('tasks'\);[\s\S]*?exitTaskTemplateEditing\(\);/,
  '切换到 Task 编排时应退出模板编辑模式',
);
assert.match(
  importFlowSource,
  /exitTaskTemplateEditing\(\);/,
  '导入 Flow JSON 后应退出模板编辑模式',
);
assert.match(
  canvasTabsSource,
  /onClick=\{\(\) => \{ setCanvasTab\('sensors'\); exitTaskTemplateEditing\(\); \}\}/,
  '切换到传感器快照时应退出模板编辑模式',
);
assert.match(
  styleSource,
  /\.canvas-context-menu\s*\{[\s\S]*?position:\s*fixed;[\s\S]*?box-sizing:\s*border-box;[\s\S]*?width:\s*176px;[\s\S]*?min-height:\s*92px;[\s\S]*?max-width:\s*calc\(100vw - 16px\);[\s\S]*?max-height:\s*calc\(100vh - 16px\);[\s\S]*?overflow:\s*auto;/,
  'Task 右键菜单应固定到浏览器视窗',
);
assert.match(
  styleSource,
  /\.canvas-context-menu button:focus-visible\s*\{/,
  'Task 右键菜单操作应提供键盘焦点样式',
);
assert.doesNotMatch(
  taskWorkspaceSource,
  /demo-action-panel|demo-canvas-toolbar|demo-right-panel/,
  'Task 工作区不应渲染流程设计三栏专属区域',
);
assert.doesNotMatch(
  canvasTabsSource,
  /Task 编排|setCanvasTab\('tasks'\)/,
  '流程设计内部切换不应再提供 Task Tab',
);
assert.match(
  taskWorkspaceSource,
  /const templateNodes = template\.nodeIds[\s\S]*?nodesById\.get\(nodeId\)/,
  'Task 模板节点解析应复用 nodesById Map',
);
assert.match(
  taskWorkspaceSource,
  /step=\{1\}[\s\S]*?setTaskSampleCount\(Math\.min\(5, Math\.max\(1, Math\.round\(Number\(event\.target\.value\)\) \|\| 1\)\)\)/,
  '样品数输入应按整数取整并钳制到 1 至 5',
);
const taskTemplates = [
  { id: 'template-a', name: '模板 A', nodeIds: ['a'], resources: ['robot'], gates: [] },
  { id: 'template-b', name: '模板 B', nodeIds: ['b'], resources: ['s07'], gates: ['s07'] },
  { id: 'template-c', name: '模板 C', nodeIds: ['c'], resources: ['s09'], gates: ['s09'] },
];
const taskInstances = [
  { id: 'a-pending', sample: 'A', templateId: 'template-a', order: 0, status: 'pending' },
  { id: 'b-running', sample: 'A', templateId: 'template-b', order: 1, status: 'running' },
  { id: 'b-done', sample: 'B', templateId: 'template-b', order: 1, status: 'done' },
  { id: 'c-waiting', sample: 'A', templateId: 'template-c', order: 2, status: 'waiting' },
];
assert.equal(typeof createEmptyTaskWorkspaceState, 'function', 'Task 工作区应导出可测试的空状态工厂');
const firstEmptyTaskWorkspace = createEmptyTaskWorkspaceState();
const secondEmptyTaskWorkspace = createEmptyTaskWorkspaceState();
assert.deepEqual(
  firstEmptyTaskWorkspace,
  { taskTemplates: [], taskInstances: [], taskEvents: [] },
  '空状态工厂应返回可直接用于清空 Task 工作区的三项状态',
);
assert.notEqual(firstEmptyTaskWorkspace.taskTemplates, secondEmptyTaskWorkspace.taskTemplates, '每次调用应返回独立模板数组');
assert.notEqual(firstEmptyTaskWorkspace.taskInstances, secondEmptyTaskWorkspace.taskInstances, '每次调用应返回独立实例数组');
assert.notEqual(firstEmptyTaskWorkspace.taskEvents, secondEmptyTaskWorkspace.taskEvents, '每次调用应返回独立事件数组');
const populatedTaskWorkspace = {
  taskTemplates: [{ id: 'template-a' }],
  taskInstances: [{ id: 'instance-a' }],
  taskEvents: ['已调度'],
};
const clearedTaskWorkspace = createEmptyTaskWorkspaceState();
assert.deepEqual(clearedTaskWorkspace, { taskTemplates: [], taskInstances: [], taskEvents: [] }, '空状态可清空已有 Task 数据');
assert.notEqual(clearedTaskWorkspace.taskTemplates, populatedTaskWorkspace.taskTemplates, '清空不能复用已有模板数组');
assert.equal(typeof resetTaskWorkspaceState, 'function', 'Task 工作区应导出可测试的重置状态变换');
const previousTaskWorkspace = {
  taskTemplates: [{ id: 'template-a' }],
  taskInstances: [{ id: 'instance-a' }],
  taskEvents: ['已调度'],
};
const resetTaskWorkspace = resetTaskWorkspaceState(previousTaskWorkspace);
assert.deepEqual(
  resetTaskWorkspace,
  { taskTemplates: [], taskInstances: [], taskEvents: [] },
  '重置状态变换应清空已有模板、实例与事件',
);
assert.deepEqual(
  previousTaskWorkspace,
  {
    taskTemplates: [{ id: 'template-a' }],
    taskInstances: [{ id: 'instance-a' }],
    taskEvents: ['已调度'],
  },
  '重置状态变换不得修改输入状态',
);
assert.notEqual(resetTaskWorkspace.taskTemplates, previousTaskWorkspace.taskTemplates, '重置状态不得复用输入模板数组');
assert.equal(
  typeof isRestorableContextMenuFocusTarget,
  'function',
  '应导出可 Node 测试的右键菜单焦点目标判定函数',
);
const bodyTarget = {};
const documentElementTarget = {};
const focusableTarget = {};
assert.equal(
  isRestorableContextMenuFocusTarget(null, bodyTarget, documentElementTarget),
  false,
  '空焦点目标不可恢复',
);
assert.equal(
  isRestorableContextMenuFocusTarget(bodyTarget, bodyTarget, documentElementTarget),
  false,
  'document.body 不可作为右键菜单焦点恢复目标',
);
assert.equal(
  isRestorableContextMenuFocusTarget(documentElementTarget, bodyTarget, documentElementTarget),
  false,
  'document.documentElement 不可作为右键菜单焦点恢复目标',
);
assert.equal(
  isRestorableContextMenuFocusTarget(focusableTarget, bodyTarget, documentElementTarget),
  true,
  '普通不同对象可作为右键菜单焦点恢复候选',
);
assert.equal(typeof clampContextMenuPosition, 'function', '应导出可 Node 测试的右键菜单坐标钳制函数');
assert.deepEqual(
  clampContextMenuPosition(490, 390, 500, 400),
  { left: 316, top: 300 },
  '右下角打开的菜单应向内钳制，避免溢出视口',
);
assert.deepEqual(
  clampContextMenuPosition(-20, -10, 500, 400),
  { left: 8, top: 8 },
  '负坐标菜单应钳制到视口安全边距',
);
assert.deepEqual(
  clampContextMenuPosition(100, 120, 500, 400),
  { left: 100, top: 120 },
  '视口中间的菜单坐标应保持不变',
);
assert.deepEqual(
  clampContextMenuPosition(100, 50, 180, 100),
  { left: 8, top: 8 },
  '极窄视口中菜单坐标应至少保留安全边距，并交由 CSS 缩小菜单尺寸',
);
assert.equal(typeof removeTaskTemplateState, 'function', 'Task 模板删除应导出可测试的纯状态变换');
const removedTaskState = removeTaskTemplateState('template-b', taskTemplates, taskInstances);
assert.deepEqual(
  removedTaskState.taskTemplates.map((template) => template.id),
  ['template-a', 'template-c'],
  '删除目标模板时应保留其他模板顺序',
);
assert.deepEqual(
  removedTaskState.taskInstances,
  [taskInstances[0], taskInstances[3]],
  '删除目标模板时应移除全部关联实例，并保留其他实例顺序和状态',
);
assert.equal(removedTaskState.removedInstanceCount, 2, '删除计数应等于实际移除的关联实例数');
const unchangedTaskState = removeTaskTemplateState('missing-template', taskTemplates, taskInstances);
assert.equal(unchangedTaskState.taskTemplates, taskTemplates, '不存在的模板不应修改模板 state');
assert.equal(unchangedTaskState.taskInstances, taskInstances, '不存在的模板不应修改实例 state');
assert.equal(unchangedTaskState.removedInstanceCount, 0, '不存在的模板不应返回删除计数');
const renderedEdgesSource = mainSource.match(
  /const renderedEdges = useMemo\([\s\S]*?\n  \}, \[edges, executionPlan\.executableEdges\]\);/,
)?.[0] || '';
assert.match(
  renderedEdgesSource,
  /executionPlan\.executableEdges\.map\(\(edge\) => JSON\.stringify\(\[edge\.source, edge\.target\]\)\)/,
  '画布应使用安全端点键识别可执行原始边',
);
assert.match(
  renderedEdgesSource,
  /executableEdgeEndpoints\.has\(JSON\.stringify\(\[edge\.source, edge\.target\]\)\)/,
  '原始边高亮判断应使用安全端点键',
);
assert.match(
  renderedEdgesSource,
  /createExecutionEdgeOverlay\(edges, executionPlan\.executableEdges\)\.map/,
  '画布应通过纯函数生成缺失的派生边覆盖层',
);
assert.match(
  renderedEdgesSource,
  /className:\s*'execution-derived-edge'[\s\S]*?animated:\s*true[\s\S]*?selectable:\s*false[\s\S]*?deletable:\s*false[\s\S]*?focusable:\s*false/,
  '派生边应动画显示且不可选择、不可删除、不可聚焦',
);
const reactFlowEdgesSource = mainSource.match(
  /<ReactFlow[\s\S]*?edges=\{renderedEdges\}[\s\S]*?nodeTypes=\{nodeTypes\}/,
)?.[0] || '';
assert.match(reactFlowEdgesSource, /edges=\{renderedEdges\}/, 'ReactFlow 应渲染原始边与派生边的组合');
assert.match(
  styleSource,
  /\.react-flow__edge\.execution-derived-edge\s+\.react-flow__edge-path\s*\{[^}]*stroke:[^;}]+;[^}]*stroke-dasharray:/,
  '派生边应有独立且清晰的连线样式',
);

const baseNodes = [
  {
    id: 'load',
    position: { x: 0, y: 0 },
    data: {
      method: 'pick_well_plate_from_loading_rack',
      label: '从上料架取孔板',
      description: '取孔板',
      params: { position: 1 },
      runStatus: 'idle',
    },
  },
];
const runningNodes = [
  {
    ...baseNodes[0],
    data: {
      ...baseNodes[0].data,
      runStatus: 'running',
    },
  },
];
const edges = [{ id: 'e1', source: 'load', target: 'unload' }];

const arrowSafeOriginalEdges = [
  { id: 'original-arrow', source: 'a->b', target: 'c' },
];
const arrowSafeExecutableEdges = [
  { id: 'same-endpoints', source: 'a->b', target: 'c' },
  { id: 'missing-endpoints', source: 'a', target: 'b->c' },
];
const overlayBaseId = 'execution-derived:1:a:4:b->c';
const collidingOriginalEdges = [
  ...arrowSafeOriginalEdges,
  { id: overlayBaseId, source: 'reserved', target: 'edge' },
];
const executionOverlay = createExecutionEdgeOverlay(
  collidingOriginalEdges,
  arrowSafeExecutableEdges,
);
assert.deepEqual(
  executionOverlay.map((edge) => [edge.source, edge.target]),
  [['a', 'b->c']],
  '覆盖层只应返回原图缺失的端点，且包含 -> 的节点 ID 不应发生端点键碰撞',
);
assert.notEqual(
  executionOverlay[0].id,
  overlayBaseId,
  '覆盖边基础 ID 与原始边冲突时应确定性消解',
);
assert.equal(
  new Set([...collidingOriginalEdges, ...executionOverlay].map((edge) => edge.id)).size,
  collidingOriginalEdges.length + executionOverlay.length,
  '原始边与覆盖边合并后 ID 应全局唯一',
);
assert.deepEqual(
  createExecutionEdgeOverlay(collidingOriginalEdges, arrowSafeExecutableEdges),
  executionOverlay,
  '相同输入应稳定生成相同覆盖边 ID',
);

assert.deepEqual(createWorkflowRequest('ai4c', baseNodes, edges), {
  name: 'ai4c',
  nodes: [
    {
      id: 'load',
      position: { x: 0, y: 0 },
      data: {
        method: 'pick_well_plate_from_loading_rack',
        label: '从上料架取孔板',
        description: '取孔板',
        params: { position: 1 },
      },
    },
  ],
  edges: [{ id: 'e1', source: 'load', target: 'unload' }],
});
assert.equal(
  workflowDraftKey('ai4c', baseNodes, edges),
  workflowDraftKey('ai4c', runningNodes, edges),
  '运行状态变化不应改变 workflow 草稿指纹',
);

const changedParamNodes = [
  {
    ...baseNodes[0],
    data: {
      ...baseNodes[0].data,
      params: { position: 2 },
    },
  },
];
assert.notEqual(
  workflowDraftKey('ai4c', baseNodes, edges),
  workflowDraftKey('ai4c', changedParamNodes, edges),
  '参数变化应触发 workflow 草稿重新校验',
);

const actionSpecs = [
  {
    method: 'pick_well_plate_from_loading_rack',
    label: '从上料架取孔板',
    description: '取孔板',
    device_id: 'robot',
    params: [{ name: 'position', label: '位置', type: 'integer', default: 1 }],
  },
  {
    method: 'put_well_plate_to_loading_rack',
    label: '放回上料架',
    description: '放孔板',
    device_id: 'robot',
    params: [{ name: 'position', label: '位置', type: 'integer', default: 2 }],
  },
];

const importedFlow = createImportedDraft(
  {
    name: 'imported_flow',
    rules: [
      {
        actions: [
          {
            action: {
              workflow_node_id: 'load',
              method: 'pick_well_plate_from_loading_rack',
              params: { position: 3 },
            },
          },
          {
            action: {
              workflow_node_id: 'unload',
              method: 'put_well_plate_to_loading_rack',
              params: { position: 4 },
            },
          },
        ],
      },
    ],
  },
  actionSpecs,
);
assert.equal(importedFlow.name, 'imported_flow');
assert.equal(importedFlow.nodes.length, 2, 'flow json 应还原两个节点');
assert.equal(importedFlow.nodes[0].data.label, '从上料架取孔板');
assert.equal(importedFlow.nodes[0].data.params.position, 3);
assert.deepEqual(
  importedFlow.edges.map((edge) => [edge.source, edge.target]),
  [['load', 'unload']],
  'flow json 应按动作顺序生成连线',
);
assert.ok(
  importedFlow.nodes[1].position.x > importedFlow.nodes[0].position.x,
  '导入 flow 后应自动生成递增横向布局',
);
const importedSnakeCaseBypassedFlow = createImportedDraft(
  {
    rules: [{
      actions: [{
        action: {
          workflow_node_id: 'load',
          method: 'pick_well_plate_from_loading_rack',
          params: {},
          execution_bypassed: true,
          execution_disabled: true,
        },
      }],
    }],
  },
  actionSpecs,
);
assert.equal(importedSnakeCaseBypassedFlow.nodes[0].data.executionBypassed, true);
assert.equal(
  importedSnakeCaseBypassedFlow.nodes[0].data.executionDisabled,
  false,
  'pseudo Flow 冲突字段应以 snake_case 直通状态优先',
);
const importedCamelCaseBypassedFlow = createImportedDraft(
  {
    rules: [{
      actions: [{
        action: {
          workflow_node_id: 'load',
          method: 'pick_well_plate_from_loading_rack',
          params: {},
          executionBypassed: true,
          executionDisabled: true,
        },
      }],
    }],
  },
  actionSpecs,
);
assert.equal(importedCamelCaseBypassedFlow.nodes[0].data.executionBypassed, true);
assert.equal(
  importedCamelCaseBypassedFlow.nodes[0].data.executionDisabled,
  false,
  'pseudo Flow 冲突字段应以 camelCase 直通状态优先',
);

const importedDraft = createImportedDraft(
  {
    name: 'canvas_draft',
    nodes: [
      {
        id: 'load',
        position: { x: 10, y: 20 },
        data: {
          method: 'pick_well_plate_from_loading_rack',
          label: '旧标签',
          description: '旧描述',
          params: { position: 5 },
        },
      },
    ],
    edges: [],
  },
  actionSpecs,
  { autoLayout: false },
);
assert.equal(importedDraft.name, 'canvas_draft');
assert.deepEqual(importedDraft.nodes[0].position, { x: 10, y: 20 }, '画布草稿可保留原坐标');
assert.equal(importedDraft.nodes[0].data.label, '从上料架取孔板', 'preset 元数据应覆盖旧标签');
assert.equal(importedDraft.nodes[0].data.params.position, 5, '导入参数应覆盖默认参数');

const restoredDraft = createImportedDraft(createWorkflowRequest('persisted_draft', importedDraft.nodes, importedDraft.edges), actionSpecs, { autoLayout: false });
assert.equal(restoredDraft.name, 'persisted_draft');
assert.equal(restoredDraft.nodes[0].id, 'load');
assert.deepEqual(restoredDraft.nodes[0].position, { x: 10, y: 20 }, '持久化草稿恢复后应保留坐标');
assert.equal(restoredDraft.nodes[0].data.params.position, 5, '持久化草稿恢复后应保留参数');

const restoredDisabledDraft = createImportedDraft(
  createWorkflowRequest(
    'persisted_disabled',
    [{ ...importedDraft.nodes[0], data: { ...importedDraft.nodes[0].data, executionDisabled: true } }],
    [],
  ),
  actionSpecs,
  { autoLayout: false },
);
assert.equal(restoredDisabledDraft.nodes[0].data.executionDisabled, true, '持久化草稿恢复后应保留禁用状态');

const bypassedWorkflowRequest = createWorkflowRequest(
  'persisted_bypassed',
  [{ ...importedDraft.nodes[0], data: { ...importedDraft.nodes[0].data, executionBypassed: true } }],
  [],
);
assert.equal(
  bypassedWorkflowRequest.nodes[0].data.execution_bypassed,
  true,
  '导出草稿时应使用 snake_case 持久化直通状态',
);
const restoredBypassedDraft = createImportedDraft(
  bypassedWorkflowRequest,
  actionSpecs,
  { autoLayout: false },
);
assert.equal(restoredBypassedDraft.nodes[0].data.executionBypassed, true, 'snake_case 草稿应恢复直通状态');
const restoredCamelCaseBypassedDraft = createImportedDraft(
  {
    ...bypassedWorkflowRequest,
    nodes: [{
      ...bypassedWorkflowRequest.nodes[0],
      data: {
        ...bypassedWorkflowRequest.nodes[0].data,
        execution_bypassed: undefined,
        executionBypassed: true,
      },
    }],
  },
  actionSpecs,
  { autoLayout: false },
);
assert.equal(
  restoredCamelCaseBypassedDraft.nodes[0].data.executionBypassed,
  true,
  'camelCase 草稿应恢复直通状态',
);
const restoredConflictingDraft = createImportedDraft(
  {
    ...bypassedWorkflowRequest,
    nodes: [{
      ...bypassedWorkflowRequest.nodes[0],
      data: {
        ...bypassedWorkflowRequest.nodes[0].data,
        execution_disabled: true,
        execution_bypassed: true,
      },
    }],
  },
  actionSpecs,
  { autoLayout: false },
);
assert.equal(restoredConflictingDraft.nodes[0].data.executionBypassed, true);
assert.equal(
  restoredConflictingDraft.nodes[0].data.executionDisabled,
  false,
  '画布草稿冲突字段应以直通状态优先',
);

const linearBypassPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.deepEqual(linearBypassPlan.executableNodes.map((node) => node.id), ['a', 'c']);
assert.deepEqual(
  linearBypassPlan.executableEdges.map((edge) => [edge.source, edge.target]),
  [['a', 'c']],
  '线性流程应越过直通节点重连前驱和后继',
);
assert.equal(linearBypassPlan.nodeStates.b.reason, 'bypassed');

const consecutiveBypassPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
);
assert.deepEqual(consecutiveBypassPlan.executableNodes.map((node) => node.id), ['a', 'd']);
assert.deepEqual(
  consecutiveBypassPlan.executableEdges.map((edge) => [edge.source, edge.target]),
  [['a', 'd']],
  '连续直通节点应收敛为一条重连边',
);
const reorderedConsecutiveBypassPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
);
assert.deepEqual(
  reorderedConsecutiveBypassPlan.executableEdges
    .map((edge) => [edge.source, edge.target, edge.id])
    .sort(),
  consecutiveBypassPlan.executableEdges
    .map((edge) => [edge.source, edge.target, edge.id])
    .sort(),
  '直通节点在 nodes 中重排后，派生边端点和 id 应保持一致',
);

const branchedBypassNodes = [
  { ...baseNodes[0], id: 'p1', data: { ...baseNodes[0].data } },
  { ...baseNodes[0], id: 'p2', data: { ...baseNodes[0].data } },
  { ...baseNodes[0], id: 'x', data: { ...baseNodes[0].data, executionBypassed: true } },
  { ...baseNodes[0], id: 'q1', data: { ...baseNodes[0].data } },
  { ...baseNodes[0], id: 'q2', data: { ...baseNodes[0].data } },
];
const branchedBypassEdges = [
  { id: 'p1-x', source: 'p1', target: 'x' },
  { id: 'p2-x', source: 'p2', target: 'x' },
  { id: 'x-q1', source: 'x', target: 'q1' },
  { id: 'x-q2', source: 'x', target: 'q2' },
  { id: 'bypass:2:p1:2:q2', source: 'p1', target: 'q1' },
  { id: 'x-p1', source: 'x', target: 'p1' },
];
const branchedBypassPlan = createExecutionPlan(branchedBypassNodes, branchedBypassEdges);
assert.deepEqual(
  branchedBypassPlan.executableEdges.map((edge) => `${edge.source}->${edge.target}`).sort(),
  ['p1->q1', 'p1->q2', 'p2->p1', 'p2->q1', 'p2->q2'],
  '分支直通应生成前驱×后继，并去重且排除自环',
);
assert.equal(
  branchedBypassPlan.executableEdges.filter((edge) => edge.source === edge.target).length,
  0,
  '派生执行图不应包含自环',
);
assert.deepEqual(
  new Set(branchedBypassPlan.executableEdges.map((edge) => edge.id)).size,
  branchedBypassPlan.executableEdges.length,
  '输入边 id 与候选派生 id 冲突时，最终执行边 id 仍应唯一',
);

const bypassedStartPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
  'b',
);
assert.equal(bypassedStartPlan.startNodeId, 'b', '直通起点仍应保留为有效选择');
assert.deepEqual(bypassedStartPlan.executableNodes.map((node) => node.id), ['c', 'd']);
assert.equal(bypassedStartPlan.nodeStates.a.reason, 'beforeStart');
assert.equal(bypassedStartPlan.nodeStates.b.reason, 'bypassed');

const executionPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data, label: 'A' } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, label: 'B' } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, label: 'C', executionDisabled: true } },
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data, label: 'D' } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
  'b',
);
assert.deepEqual(executionPlan.executableNodes.map((node) => node.id), ['b']);
assert.deepEqual(executionPlan.executableEdges, []);
assert.equal(executionPlan.nodeStates.a.reason, 'beforeStart');
assert.equal(executionPlan.nodeStates.b.reason, 'willRun');
assert.equal(executionPlan.nodeStates.c.reason, 'disabled');
assert.equal(executionPlan.nodeStates.d.reason, 'blockedByDisabled');
assert.equal(executionPlan.startNodeId, 'b');
assert.equal(executionPlan.disabledNodeId, 'c');

const disabledBeforeStartPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data, executionDisabled: true } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
  'b',
);
assert.deepEqual(disabledBeforeStartPlan.executableNodes.map((node) => node.id), ['b', 'c']);
assert.equal(disabledBeforeStartPlan.nodeStates.a.reason, 'beforeStart');
assert.equal(disabledBeforeStartPlan.disabledNodeId, null);

const unorderedDagPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, label: 'B' } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, label: 'C' } },
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data, label: 'A' } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.deepEqual(
  unorderedDagPlan.executableNodes.map((node) => node.id),
  ['a', 'b', 'c'],
  '执行计划应按 DAG 拓扑顺序，而不是节点加入顺序',
);

const laidOut = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'a', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'b', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'c', position: { x: 999, y: 999 } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.ok(laidOut[1].position.x > laidOut[0].position.x, '线性流程应按 x 轴递增布局');
assert.ok(laidOut[2].position.x > laidOut[1].position.x, '线性流程后续节点应继续右移');

const unorderedDagLayout = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'b', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'c', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'a', position: { x: 999, y: 999 } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.deepEqual(
  unorderedDagLayout.map((node) => node.id),
  ['a', 'b', 'c'],
  '自动布局应按 DAG 拓扑顺序重排节点数组，而不是保留加入顺序',
);
assert.ok(unorderedDagLayout[1].position.x > unorderedDagLayout[0].position.x, 'DAG 后继节点应排在前驱右侧');

const gridLayout = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'first', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'second', position: { x: 999, y: 999 } },
  ],
  [],
);
assert.notDeepEqual(gridLayout[0].position, gridLayout[1].position, '无边节点应分配不同网格位置');

const wrappedLinearLayout = layoutFlowGraph(
  Array.from({ length: 7 }, (_, index) => ({
    ...baseNodes[0],
    id: `node_${index + 1}`,
    position: { x: 999, y: 999 },
  })),
  Array.from({ length: 6 }, (_, index) => ({
    id: `edge_${index + 1}`,
    source: `node_${index + 1}`,
    target: `node_${index + 2}`,
  })),
);
assert.equal(wrappedLinearLayout[6].position.x, wrappedLinearLayout[0].position.x, '第 7 个节点应换行回到行首');
assert.ok(wrappedLinearLayout[6].position.y > wrappedLinearLayout[0].position.y, '第 7 个节点应排到下一行');

const branchedLayout = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'precheck', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'solvent', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'branch_start', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 's05', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'photo', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 's11', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 's08', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'join', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'post', position: { x: 999, y: 999 } },
  ],
  [
    { id: 'precheck-solvent', source: 'precheck', target: 'solvent' },
    { id: 'solvent-branch', source: 'solvent', target: 'branch_start' },
    { id: 'branch-s05', source: 'branch_start', target: 's05' },
    { id: 'branch-s11', source: 'branch_start', target: 's11' },
    { id: 's05-photo', source: 's05', target: 'photo' },
    { id: 'photo-join', source: 'photo', target: 'join' },
    { id: 's11-s08', source: 's11', target: 's08' },
    { id: 's08-join', source: 's08', target: 'join' },
    { id: 'join-post', source: 'join', target: 'post' },
  ],
);
const branchedById = Object.fromEntries(branchedLayout.map((node) => [node.id, node.position]));
assert.equal(branchedById.solvent.y, branchedById.precheck.y, '分支前主干应保留在同一行');
assert.ok(branchedById.solvent.x > branchedById.precheck.x, '分支前主干应横向递增');
assert.equal(branchedById.branch_start.x, branchedById.precheck.x, '分支入口锚点应另起一行并回到行首');
assert.ok(branchedById.branch_start.y > branchedById.precheck.y, '分支入口锚点应下移到新行');
assert.equal(branchedById.photo.y, branchedById.s05.y, '同一条分支应按横向行排列');
assert.ok(branchedById.photo.x > branchedById.s05.x, '同一条分支后续节点应向右排列');
assert.equal(branchedById.s08.y, branchedById.s11.y, '第二条分支也应按横向行排列');
assert.ok(branchedById.s08.x > branchedById.s11.x, '第二条分支后续节点应向右排列');
assert.ok(branchedById.s11.y > branchedById.s05.y, '不同分支应分到不同横向行');
assert.equal(branchedById.join.y, branchedById.branch_start.y, '分支汇合锚点应回到分支入口所在主干行');
assert.ok(branchedById.join.x > branchedById.branch_start.x, '分支汇合锚点应位于分支入口右侧');
assert.equal(branchedById.post.y, branchedById.branch_start.y, '汇合后的主干应继续沿锚点行排列');
assert.ok(branchedById.post.x > branchedById.join.x, '汇合后的主干应继续向右排列');

const opcRowsWhileRunning = collectOpcChanges([
  {
    sequence: 1,
    message: 'OPC状态采样: 2 个变量',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      before: {
        S06允许加工: {
          name: 'S06允许加工',
          label: 'S06允许加工',
          display_name: 'S06允许加工',
          node_id: 'ns=2;i=269',
          value: { success: true, value: true, node_id: 'ns=2;i=269' },
          value_goal: { success: true, value: false, node_id: 'ns=2;i=269' },
        },
        S06加工完成: {
          name: 'S06加工完成',
          label: 'S06加工完成',
          display_name: 'S06加工完成',
          node_id: 'ns=2;i=270',
          value: { success: true, value: false, node_id: 'ns=2;i=270' },
        },
      },
    },
  },
]);
assert.equal(opcRowsWhileRunning.length, 2, '运行中应显示执行前采样到的等待变量');
assert.equal(opcRowsWhileRunning[0].valueBegin.value, true);
assert.equal(opcRowsWhileRunning[0].valueGoal.value, false);
assert.equal(opcRowsWhileRunning[0].valueEnd, undefined);

const opcRowsWithWaitGoal = collectOpcChanges([
  {
    sequence: 1,
    message: 'OPC状态采样: 1 个变量',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      before: {
        S06加工完成: {
          name: 'S06加工完成',
          label: 'S06加工完成',
          display_name: 'S06加工完成',
          node_id: 'ns=4;s=S06加工完成',
          value: { success: true, value: false, node_id: 'ns=4;s=S06加工完成' },
        },
      },
    },
  },
  {
    sequence: 2,
    message: '等待 OPC 变量 S06加工完成 == true',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      type: 'opc_wait',
      phase: 'start',
      variable: 'S06加工完成',
      expected: true,
      node_id: 'ns=4;s=S06加工完成',
      display_name: 'S06加工完成',
      label: 'S06加工完成 (ns=4;s=S06加工完成)',
    },
  },
  {
    sequence: 3,
    message: 'OPC 变量等待完成 S06加工完成 == true',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      type: 'opc_wait',
      phase: 'finish',
      variable: 'S06加工完成',
      expected: true,
      last_value: true,
      node_id: 'ns=4;s=S06加工完成',
      display_name: 'S06加工完成',
      label: 'S06加工完成 (ns=4;s=S06加工完成)',
    },
  },
]);
assert.equal(opcRowsWithWaitGoal.length, 1, 'wait expected 应合并到同一个 OPC 变量行');
assert.equal(opcRowsWithWaitGoal[0].valueBegin.value, false);
assert.equal(opcRowsWithWaitGoal[0].valueGoal, true);
assert.equal(opcRowsWithWaitGoal[0].valueEnd, true);

assert.equal(formatOpcValue({ success: true, value: false, node_id: 'ns=2;i=270' }), 'false');
assert.equal(formatOpcValue({ success: false, error: 'bad node' }), 'bad node');

assert.equal(
  formatUiError(new TypeError('Failed to fetch'), '运行 workflow'),
  '运行 workflow 失败：无法连接本地调试服务，请确认 workflow_ui 后端仍在运行，且当前页面与后端端口一致。',
);
assert.equal(formatUiError(new Error('workflow 不能包含环'), '校验流程'), '校验流程失败：workflow 不能包含环');

const summary = buildWorkspaceSummary({
  nodes: [
    { data: { deviceId: 'szlab_mixer_robot', runStatus: 'success' } },
    { data: { deviceId: 'szlab_mixer_stirrer', runStatus: 'running' } },
    { data: { deviceId: 'szlab_mixer_photoshotting', runStatus: 'idle' } },
  ],
  edges: [{}, {}],
  opcChangeCount: 6,
  runStatus: 'running',
});
assert.deepEqual(summary, {
  totalNodes: 3,
  totalEdges: 2,
  runningNodes: 1,
  completedNodes: 1,
  deviceCount: 3,
  opcChangeCount: 6,
  runStatusText: '运行中',
});

assert.deepEqual(
  groupActionsByDevice([
    {
      method: 'submit_place_to_s04',
      label: '放置到 S04 磁搅位',
      description: '放置到 S04 磁搅位',
      device_id: 'szlab_mixer_robot',
    },
    {
      method: 'run_stirring',
      label: '执行 S04 磁搅加工',
      description: '执行 S04 磁搅加工',
      device_id: 'szlab_mixer_stirrer',
    },
    {
      method: 'take_photo',
      label: '拍照并保存结果',
      description: '拍照并保存结果',
      device_id: 'szlab_mixer_photoshotting',
    },
  ]),
  [
    {
      id: 'szlab_mixer_robot',
      title: '机械臂转运',
      device: 'szlab_mixer_robot',
      actions: [
        {
          method: 'submit_place_to_s04',
          label: '放置到 S04 磁搅位',
          description: '放置到 S04 磁搅位',
          device_id: 'szlab_mixer_robot',
        },
      ],
    },
    {
      id: 'szlab_mixer_stirrer',
      title: 'szlab_mixer_stirrer',
      device: 'szlab_mixer_stirrer',
      actions: [
        {
          method: 'run_stirring',
          label: '执行 S04 磁搅加工',
          description: '执行 S04 磁搅加工',
          device_id: 'szlab_mixer_stirrer',
        },
      ],
    },
    {
      id: 'szlab_mixer_photoshotting',
      title: 'szlab_mixer_photoshotting',
      device: 'szlab_mixer_photoshotting',
      actions: [
        {
          method: 'take_photo',
          label: '拍照并保存结果',
          description: '拍照并保存结果',
          device_id: 'szlab_mixer_photoshotting',
        },
      ],
    },
  ],
  '动作面板应按机械臂和单设备分层显示',
);
