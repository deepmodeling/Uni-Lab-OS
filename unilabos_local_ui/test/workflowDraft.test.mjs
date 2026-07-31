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
  annotateTaskGanttEntries,
  buildTaskGanttEntries,
  canDeleteTaskTemplate,
  createDefaultTriggerCondition,
  createOperationGenerationController,
  createSynchronousActionGate,
  createTaskTemplateDraft,
  createTaskTemplateId,
  createWorkspaceEpochController,
  isTaskWaitingStatus,
  normalizeTriggerConditions,
  renameTaskTemplate,
  resolveTaskTemplateNameDraft,
  resolveTemplateNodes,
  inferMethodFromTemplateNodeId,
  taskLocalWaitingReason,
  taskTemplateDeviceIds,
  updateScheduledTemplateDraft,
  updateTaskTemplateTriggers,
} = await importTypeScriptModule(
  new URL('../src/taskOrchestration.ts', import.meta.url),
);
const {
  createTaskOrchestrationClient,
  createTaskExecutionController,
  createTaskExecutionStatus,
  fromApiTrigger,
  resolveTaskOrchestrationApiUrl,
  pauseTaskSchedulerReliably,
  runTaskExecutionHarvestCycle,
  runTaskExecutionCycle,
  runTaskSchedulerTransition,
  TaskOrchestrationBusinessError,
  TaskExecutionCycleCancelledError,
  TaskOrchestrationServiceUnavailableError,
  toApiTrigger,
} = await importTypeScriptModule(
  new URL('../src/taskOrchestrationApi.ts', import.meta.url),
);
const {
  addProfileCondition,
  addProfileVariable,
  addProfileWrite,
  beginOpcSimulatorControlOperation,
  buildOpcActionCatalog,
  buildOpcVariableTypeCatalog,
  canonicalProfileJson,
  clearProfileVariableInitialValue,
  collectScheduledTemplateIds,
  countNodeMissingFields,
  createCanonicalProfileBlob,
  createLatestOperationGate,
  createOpcSimulatorClient,
  createProfileSaveSnapshot,
  DEFAULT_OPC_SIMULATOR_URL,
  defaultOpcSimulatorFileName,
  finishOpcSimulatorControlOperation,
  formatJsonScalar,
  isProfileSaveSnapshotCurrent,
  isSimulatorStartAllowed,
  opcSimulatorStopMessage,
  parseOpcSimulatorProfileJson,
  parseJsonScalar,
  removeProfileCondition,
  removeProfileVariable,
  removeProfileWrite,
  updateProfileCondition,
  updateProfileVariable,
  updateProfileWrite,
  validateOpcSimulatorProfile,
} = await importTypeScriptModule(
  new URL('../src/opcSimulatorProfile.ts', import.meta.url),
);
const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
const taskOrchestrationSource = await readFile(new URL('../src/taskOrchestration.ts', import.meta.url), 'utf8');
const taskOrchestrationApiSource = await readFile(new URL('../src/taskOrchestrationApi.ts', import.meta.url), 'utf8');
const styleSource = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
const opcSimulatorDialogSource = await readFile(new URL('../src/OpcSimulatorDialog.tsx', import.meta.url), 'utf8');
const opcChangesSource = await readFile(new URL('../src/opcChanges.ts', import.meta.url), 'utf8');
const taskSchedulerBenchSource = await readFile(new URL('../src/TaskSchedulerBench.tsx', import.meta.url), 'utf8');
assert.match(
  mainSource,
  /const \[taskUtilityDrawer, setTaskUtilityDrawer\] = useState<'opc-connection' \| null>\(null\);/,
  '只有 Task OPC 连接应使用工具抽屉，测试配置与队列必须常驻主区',
);
assert.match(
  mainSource,
  /const \[taskExecutionEnvironment, setTaskExecutionEnvironment\] = useState<'simulated' \| 'real'>\('simulated'\);/,
  'Task 调试台必须显式区分模拟 OPC 与真实执行环境',
);
assert.match(
  mainSource,
  /const \[taskTemplateDrawerTab, setTaskTemplateDrawerTab\] = useState<'templates' \| 'scheduled'>\('templates'\);/,
  '模板抽屉应组合模板库与待排模板',
);
assert.match(
  mainSource,
  /const \[taskLogTab, setTaskLogTab\] = useState<'waiting' \| 'events' \| 'action'>\('waiting'\);/,
  '右侧日志应通过 Tab 组合等待、事件和 Action 日志',
);
assert.match(
  mainSource,
  /const \[isOpcSimulatorDrawerOpen, setIsOpcSimulatorDrawerOpen\] = useState\(false\);/,
  'OPC 模拟器应使用独立抽屉开关状态',
);
assert.match(
  mainSource,
  /className="task-workspace-commandbar"[\s\S]*?setTaskExecutionEnvironment\('simulated'\)[\s\S]*?setTaskExecutionEnvironment\('real'\)[\s\S]*?setTaskUtilityDrawer\('opc-connection'\)[\s\S]*?setIsOpcSimulatorDrawerOpen\(true\)/,
  'Task 命令栏应提供环境切换、Task OPC 与模拟器入口',
);
assert.match(
  mainSource,
  /className="task-orchestration task-focus-layout task-debug-bench"/,
  'Task 主页面应使用演示稿定义的三栏调试台布局',
);
assert.match(mainSource, /className="task-column task-test-config"/, 'Task 测试配置必须常驻左侧栏');
assert.match(
  mainSource,
  /className="task-column task-queue-column"/,
  'Task 队列必须常驻在主区',
);
assert.doesNotMatch(
  mainSource,
  /taskUtilityDrawer === 'queue'/,
  '常驻队列不能再依赖工具抽屉状态',
);
assert.match(
  mainSource,
  /className=\{`task-opc-connection-drawer task-utility-drawer\$\{taskUtilityDrawer === 'opc-connection' \? ' open' : ''\}`\}/,
  'Task OPC 连接应使用与模拟器一致的右侧抽屉',
);
assert.match(
  mainSource,
  /className=\{`task-opc-drawer\$\{isOpcSimulatorDrawerOpen \? ' open' : ''\}`\}/,
  'OPC 模拟器内容应渲染在抽屉容器内',
);
assert.match(
  styleSource,
  /\.task-opc-drawer\s*\{[\s\S]*?background:\s*#f8fafc;/,
  'OPC 模拟器抽屉应复用工作区浅色表面',
);
assert.match(
  styleSource,
  /\.task-focus-layout\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(0,\s*1fr\);/,
  'Task 主页面应以队列与日志作为等宽双主列',
);
assert.match(
  mainSource,
  /<section className="task-column task-sample-strip">[\s\S]*?<div[\s\S]*?'task-sample-block'[\s\S]*?title=\{`\$\{block\.templateName\} · \$\{block\.actionDone\}\/\$\{block\.actionTotal\}`\}/,
  '样品进度只能作为只读缩略图展示',
);
assert.doesNotMatch(
  mainSource,
  /task-sample-block[\s\S]*?onClick=\{\(\) => \{[\s\S]*?setTaskLogTab\('action'\)/,
  '只读进度缩略图不得切换日志或改变选中任务',
);
assert.match(
  mainSource,
  /taskExecutionEnvironment === 'real'[\s\S]*?disabled=\{taskExecutionEnvironment === 'real'\}[\s\S]*?OPC 模拟器/,
  '真实执行环境必须禁用 OPC 模拟器入口',
);
assert.match(
  mainSource,
  /className="task-orchestration task-focus-layout task-debug-bench"[\s\S]*?className="task-column task-test-config"[\s\S]*?className="task-column task-queue-column"[\s\S]*?className="task-column task-log-column"/,
  '调试台必须按演示稿保持测试配置、队列和日志三栏结构',
);
assert.match(
  styleSource,
  /\.task-debug-bench\s*\{[\s\S]*?grid-template-columns:\s*240px\s+minmax\(420px,\s*1fr\)\s+minmax\(360px,\s*0\.88fr\);/,
  '调试台三栏尺寸必须以演示稿为准，避免被旧工作区样式挤压',
);
assert.match(
  mainSource,
  /<h2>本次测试配置<\/h2>[\s\S]*?样品数[\s\S]*?选择 Task 模板（按顺序执行）[\s\S]*?OPC 环境/,
  '左侧必须保留演示稿中的测试配置入口',
);
assert.match(
  mainSource,
  /import \{ TaskSchedulerBench \} from '\.\/TaskSchedulerBench';[\s\S]*?<TaskSchedulerBench/,
  'Task 工作区必须改用独立联调台组件，而不是继续拼装旧工作区 JSX',
);
assert.match(
  taskSchedulerBenchSource,
  /下载已选[\s\S]*?删除已选[\s\S]*?清空全部[\s\S]*?onDownloadTemplate[\s\S]*?onDeleteTemplate/,
  '模板库必须同时提供批量下载/删除和单模板下载/删除',
);
assert.match(
  mainSource,
  /schema: 'unilabos\.task-templates'[\s\S]*?source_workflow_path[\s\S]*?input_triggers[\s\S]*?output_triggers/,
  '模板下载 JSON 必须携带版本、来源、节点和触发条件',
);
assert.match(
  mainSource,
  /taskApiRef\.current\.deleteTemplates\([\s\S]*?templates\.map\(\(template\) => template\.id\)/,
  '批量模板删除必须通过原子 API 完成',
);
assert.match(
  mainSource,
  /className="task-template-drawer-tabs"[\s\S]*?taskTemplateDrawerTab === 'templates'[\s\S]*?taskTemplateDrawerTab === 'scheduled'/,
  '模板抽屉应使用 Tab 切换模板库与待排模板',
);
assert.match(
  mainSource,
  /className="task-log-tabs"[\s\S]*?taskLogTab === 'waiting'[\s\S]*?taskLogTab === 'events'[\s\S]*?taskLogTab === 'action'/,
  '日志栏应使用 Tab 切换三类日志',
);
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

const opcProfileFixture = {
  schema_version: 2,
  status: 'draft',
  name: 'line-a',
  opc: {
    url: 'opc.tcp://127.0.0.1:4840',
    poll_interval: 0.2,
    io_timeout: 2,
  },
  variables: [
    { name: 'command', direction: 'pc_to_plc', data_type: 'int', source: 'manual' },
    { name: 'done', direction: 'plc_to_pc', data_type: 'bool', initial_value: false, source: 'task_output' },
  ],
  nodes: [{
    workflow_node_id: 'node-a',
    task_template_ids: ['task-a'],
    device_id: 'device-a',
    method: 'run',
    params: {},
    channel: '',
    trigger: { all: [] },
    on_trigger: { writes: [] },
    on_complete: { delay: 0.5, writes: [] },
    reset_when: null,
    after_reset: null,
  }],
};
const condition = { variable: 'command', operator: 'eq', edge: 'rising', value: 1 };

assert.equal(
  DEFAULT_OPC_SIMULATOR_URL,
  'opc.tcp://127.0.0.1:4840',
  'OPC 模拟器默认地址必须是零远程写风险的 loopback endpoint',
);
assert.match(
  mainSource,
  /const DEFAULT_CONFIG = \{[\s\S]*?url: DEFAULT_OPC_SIMULATOR_URL,/,
  'Task OPC 与模拟器默认地址必须引用同一安全常量',
);
assert.match(
  mainSource,
  /placeholder=\{DEFAULT_OPC_SIMULATOR_URL\}/,
  'OPC URL 输入提示也必须使用安全 loopback 常量',
);

assert.deepEqual(
  collectScheduledTemplateIds(['task-a', 'task-b', 'task-a', '', 'task-c']),
  ['task-a', 'task-b', 'task-c'],
  'Resource Schedule 模板 ID 必须去重并保持拖入顺序',
);
for (const [text, expected] of [
  ['true', true],
  ['false', false],
  ['null', null],
  ['12', 12],
  ['"12"', '12'],
  ['"中文"', '中文'],
]) {
  assert.deepEqual(parseJsonScalar(text), expected, `必须严格解析 JSON scalar：${text}`);
  assert.equal(formatJsonScalar(expected), text, `必须规范格式化 JSON scalar：${text}`);
}
for (const invalid of ['', '01', 'NaN', '{}', '[]', 'undefined']) {
  assert.throws(() => parseJsonScalar(invalid), /JSON scalar/, `必须拒绝非 JSON scalar：${invalid}`);
}
assert.throws(
  () => parseOpcSimulatorProfileJson('{"schema_version":2,"nodes":{}}'),
  /profile/,
  '高级 JSON Apply 必须拒绝结构不完整的对象，避免编辑器运行时崩溃',
);
assert.deepEqual(
  parseOpcSimulatorProfileJson(JSON.stringify(opcProfileFixture)),
  opcProfileFixture,
  '高级 JSON Apply 必须接受完整 schema v2 对象',
);
assert.deepEqual(
  validateOpcSimulatorProfile({
    ...opcProfileFixture,
    opc: { ...opcProfileFixture.opc, poll_interval: 0.01, io_timeout: 61 },
  }).map((error) => error.path).filter((path) => path.startsWith('opc.')),
  ['opc.poll_interval', 'opc.io_timeout'],
  '本地校验必须与后端一致拒绝越界 timing',
);
assert.deepEqual(
  validateOpcSimulatorProfile({
    ...opcProfileFixture,
    variables: [{ ...opcProfileFixture.variables[0], source: 'input' }],
  }).map((error) => error.path).filter((path) => path.includes('.source')),
  ['variables[0].source'],
  'source 必须严格限制为 action_node/task_input/task_output/manual',
);
const unknownActionProfile = {
  ...opcProfileFixture,
  variables: [{
    name: 'action-ready',
    direction: 'unknown',
    data_type: 'unknown',
    source: 'action_node',
  }],
};
assert.deepEqual(
  parseOpcSimulatorProfileJson(JSON.stringify(unknownActionProfile)),
  unknownActionProfile,
  '高级 JSON Apply 必须接受 action_node source 与 unknown 草稿字段',
);
assert.deepEqual(
  validateOpcSimulatorProfile(unknownActionProfile)
    .map((error) => error.path)
    .filter((path) => path.startsWith('variables[0].')),
  ['variables[0].direction', 'variables[0].data_type'],
  'action_node unknown 草稿必须只报告待补方向与类型，不得误报 source',
);
assert.deepEqual(
  buildOpcVariableTypeCatalog([
    { name: 'bool-value', data_type: 'BOOL' },
    { name: 'int-value', data_type: 'integer' },
    { name: 'float-value', data_type: 'Double' },
    { name: 'text-value', data_type: 'STRING' },
    { name: 'ambiguous', data_type: 'BOOL' },
    { name: 'ambiguous', data_type: 'INT' },
    { name: 'unsupported', data_type: 'bytes' },
  ]),
  [
    { name: 'bool-value', data_type: 'bool' },
    { name: 'int-value', data_type: 'int' },
    { name: 'float-value', data_type: 'float' },
    { name: 'text-value', data_type: 'string' },
  ],
  'CSV catalog 仅可用唯一名称和严格已知类型补齐 Action 变量类型',
);
assert.deepEqual(
  buildOpcActionCatalog([
    {
      device_id: 'device-a',
      method: 'run',
      opc_variables: ['ready'],
    },
  ]),
  [{
    device_id: 'device-a',
    method: 'run',
    opc_variables: ['ready'],
  }],
  'generate Action catalog 必须保留原始严格字段',
);
assert.throws(
  () => buildOpcActionCatalog([
    {
      method: 'run',
      opc_variables: ['ready'],
    },
  ]),
  /动作 run 缺少 device_id/,
  'generate 前必须阻止缺少 device_id 的 Action，不能发送非法 catalog',
);
assert.throws(
  () => buildOpcActionCatalog([
    {
      device_id: ' ',
      method: 'run',
      opc_variables: [],
    },
  ]),
  /动作 run 缺少 device_id/,
  '空白 device_id 也必须在请求前阻止',
);

const pollGate = createLatestOperationGate();
const stalePoll = pollGate.begin();
const latestPoll = pollGate.begin();
assert.equal(stalePoll.signal.aborted, true, '新 poll 必须 abort 前一个请求');
assert.equal(pollGate.isCurrent(stalePoll.generation), false, '乱序旧 poll 不得应用');
assert.equal(pollGate.isCurrent(latestPoll.generation), true);
pollGate.invalidate();
assert.equal(latestPoll.signal.aborted, true, 'start/stop 必须可 abort 当前 poll');
assert.equal(pollGate.isCurrent(latestPoll.generation), false, 'start 后旧 poll 不得覆盖新 run_id');
const afterOperation = pollGate.begin();
pollGate.unmount();
assert.equal(afterOperation.signal.aborted, true, 'unmount 必须 abort 状态请求');
assert.equal(pollGate.isCurrent(afterOperation.generation), false);

const controlInFlight = { current: false };
const controlToken = { current: 0 };
let startCalls = 0;
let stopCalls = 0;
let ownedControlRunId = null;
let releaseStart;
const startResponse = new Promise((resolve) => { releaseStart = resolve; });
const simulatedStart = async () => {
  if (controlInFlight.current) return;
  const operation = beginOpcSimulatorControlOperation(controlInFlight, controlToken);
  if (operation === null) return;
  try {
    startCalls += 1;
    const status = await startResponse;
    if (operation === controlToken.current) ownedControlRunId = status.run_id;
  } finally {
    finishOpcSimulatorControlOperation(controlInFlight, controlToken, operation);
  }
};
const simulatedStop = async () => {
  if (controlInFlight.current) return;
  const operation = beginOpcSimulatorControlOperation(controlInFlight, controlToken);
  if (operation === null) return;
  try {
    stopCalls += 1;
  } finally {
    finishOpcSimulatorControlOperation(controlInFlight, controlToken, operation);
  }
};
const firstStart = simulatedStart();
await simulatedStart();
await simulatedStop();
assert.equal(startCalls, 1, '双 start 只能调用一次 API');
assert.equal(stopCalls, 0, 'start 期间 stop 必须同步忽略');
releaseStart({ run_id: 'c'.repeat(32) });
await firstStart;
assert.equal(ownedControlRunId, 'c'.repeat(32), '成功 start 必须保存 owned run_id');

let releaseStop;
const stopResponse = new Promise((resolve) => { releaseStop = resolve; });
const delayedStop = async () => {
  if (controlInFlight.current) return;
  const operation = beginOpcSimulatorControlOperation(controlInFlight, controlToken);
  if (operation === null) return;
  try {
    stopCalls += 1;
    await stopResponse;
  } finally {
    finishOpcSimulatorControlOperation(controlInFlight, controlToken, operation);
  }
};
const firstStop = delayedStop();
await delayedStop();
assert.equal(stopCalls, 1, '双 stop 只能调用一次 API');
releaseStop();
await firstStop;

const saveSnapshot = createProfileSaveSnapshot(opcProfileFixture, 'runnable', 'line-a.json');
assert.equal(
  isProfileSaveSnapshotCurrent(saveSnapshot, opcProfileFixture, 'line-a.json'),
  true,
);
assert.equal(
  isProfileSaveSnapshotCurrent(
    saveSnapshot,
    { ...opcProfileFixture, name: 'edited while saving' },
    'line-a.json',
  ),
  false,
  '保存期间发生编辑时旧响应不得覆盖当前输入',
);
assert.equal(
  isProfileSaveSnapshotCurrent(saveSnapshot, opcProfileFixture, 'renamed.json'),
  false,
  '保存期间修改文件名时旧响应不得清除 dirty',
);

for (const [candidate, expectedPath] of [
  [{ ...opcProfileFixture, extra: true }, 'extra'],
  [{ ...opcProfileFixture, opc: { ...opcProfileFixture.opc, extra: true } }, 'opc.extra'],
  [{ ...opcProfileFixture, variables: [{ ...opcProfileFixture.variables[0], extra: true }] }, 'variables[0].extra'],
  [{ ...opcProfileFixture, nodes: [{ ...opcProfileFixture.nodes[0], extra: true }] }, 'nodes[0].extra'],
  [{
    ...opcProfileFixture,
    nodes: [{
      ...opcProfileFixture.nodes[0],
      trigger: { all: [{ ...condition, extra: true }] },
    }],
  }, 'nodes[0].trigger.all[0].extra'],
]) {
  assert.ok(
    validateOpcSimulatorProfile(candidate).some((error) => error.path === expectedPath),
    `local validator 必须拒绝未知字段：${expectedPath}`,
  );
}
const duplicateNodeProfile = {
  ...opcProfileFixture,
  nodes: [
    { ...opcProfileFixture.nodes[0], channel: 'a', trigger: { all: [condition] } },
    { ...opcProfileFixture.nodes[0], channel: 'b', trigger: { all: [condition] } },
  ],
};
assert.ok(validateOpcSimulatorProfile(duplicateNodeProfile)
  .some((error) => error.path === 'nodes[1].workflow_node_id'));
assert.ok(validateOpcSimulatorProfile({
  ...opcProfileFixture,
  nodes: [{ ...opcProfileFixture.nodes[0], task_template_ids: [''] }],
}).some((error) => error.path === 'nodes[0].task_template_ids[0]'));
const missingDelayProfile = JSON.parse(JSON.stringify(opcProfileFixture));
delete missingDelayProfile.nodes[0].on_complete.delay;
assert.ok(validateOpcSimulatorProfile(missingDelayProfile)
  .some((error) => error.path === 'nodes[0].on_complete.delay'));
const missingScalarProfile = JSON.parse(JSON.stringify(opcProfileFixture));
missingScalarProfile.nodes[0].trigger = {
  all: [{ variable: 'missing', operator: 'eq', edge: 'level' }],
};
missingScalarProfile.nodes[0].on_trigger = { writes: [{ variable: 'missing' }] };
const missingScalarErrors = validateOpcSimulatorProfile(missingScalarProfile);
assert.ok(missingScalarErrors.some((error) => error.path === 'nodes[0].trigger.all[0].value'));
assert.ok(missingScalarErrors.some((error) => error.path === 'nodes[0].on_trigger.writes[0].value'));
const maliciousProfile = JSON.parse(JSON.stringify(opcProfileFixture));
maliciousProfile.nodes[0].params = JSON.parse('{"constructor":{"polluted":true}}');
assert.ok(validateOpcSimulatorProfile(maliciousProfile)
  .some((error) => error.path === 'nodes[0].params.constructor'));
assert.throws(
  () => parseOpcSimulatorProfileJson(JSON.stringify(maliciousProfile)),
  /禁止字段/,
  '高级 JSON 必须在 Apply 前拒绝递归原型污染键',
);
assert.throws(
  () => parseOpcSimulatorProfileJson(`${'['.repeat(101)}0${']'.repeat(101)}`),
  /嵌套/,
  '高级 JSON 必须在 parse 前拒绝超过 100 层嵌套',
);
assert.throws(
  () => parseOpcSimulatorProfileJson(`"${'x'.repeat(2 * 1024 * 1024)}"`),
  /2 MiB/,
  '高级 JSON 必须在 parse 前拒绝超过 2 MiB',
);
assert.ok(validateOpcSimulatorProfile({
  ...opcProfileFixture,
  variables: Array.from({ length: 501 }, (_, index) => ({
    name: `v-${index}`,
    direction: 'pc_to_plc',
    data_type: 'bool',
    source: 'manual',
  })),
}).some((error) => error.path === 'variables'));
assert.throws(
  () => canonicalProfileJson({ ...opcProfileFixture, invalid: undefined }),
  /JSON data/,
  'canonicalizer 必须拒绝不能无损表达的非 JSON data',
);
assert.equal(
  await createCanonicalProfileBlob(opcProfileFixture).text(),
  `${JSON.stringify(opcProfileFixture, null, 2)}\n`,
  '下载 Blob 必须直接使用 canonical profile 文本并保留末尾换行',
);

const localProfileErrors = validateOpcSimulatorProfile(opcProfileFixture);
assert.ok(localProfileErrors.some((error) => error.path === 'nodes[0].channel' && error.nodeId === 'node-a'));
assert.ok(localProfileErrors.some((error) => error.path === 'nodes[0].trigger.all' && error.nodeId === 'node-a'));
assert.equal(countNodeMissingFields(opcProfileFixture.nodes[0], opcProfileFixture.variables), 2);

const withVariable = addProfileVariable(opcProfileFixture, {
  name: 'ready',
  direction: 'plc_to_pc',
  data_type: 'bool',
  initial_value: true,
  source: 'manual',
});
assert.equal(opcProfileFixture.variables.length, 2, '变量新增不得修改原对象');
assert.equal(withVariable.variables.length, 3);
const updatedVariable = updateProfileVariable(withVariable, 2, { direction: 'pc_to_plc' });
assert.equal(withVariable.variables[2].direction, 'plc_to_pc', '变量更新必须不可变');
assert.equal(updatedVariable.variables[2].direction, 'pc_to_plc');
const clearedVariable = clearProfileVariableInitialValue(withVariable, 2);
assert.equal(Object.hasOwn(withVariable.variables[2], 'initial_value'), true, '清空不得修改原变量');
assert.equal(Object.hasOwn(clearedVariable.variables[2], 'initial_value'), false, '清空必须真正删除 own property');
assert.doesNotThrow(() => canonicalProfileJson(clearedVariable));
const undefinedPatchedVariable = updateProfileVariable(withVariable, 2, { initial_value: undefined });
assert.equal(
  Object.hasOwn(undefinedPatchedVariable.variables[2], 'initial_value'),
  false,
  'update patch 中的 undefined 必须删除字段，不能进入 JSON profile',
);
assert.doesNotThrow(() => canonicalProfileJson(undefinedPatchedVariable));
assert.equal(removeProfileVariable(updatedVariable, 2).variables.length, 2);

const withCondition = addProfileCondition(opcProfileFixture, 0, 'trigger', condition);
assert.equal(opcProfileFixture.nodes[0].trigger.all.length, 0, '条件新增不得修改原对象');
assert.deepEqual(updateProfileCondition(withCondition, 0, 'trigger', 0, { value: 2 }).nodes[0].trigger.all[0].value, 2);
assert.equal(removeProfileCondition(withCondition, 0, 'trigger', 0).nodes[0].trigger.all.length, 0);

const write = { variable: 'done', value: true };
const withWrite = addProfileWrite(opcProfileFixture, 0, 'on_complete', write);
assert.equal(opcProfileFixture.nodes[0].on_complete.writes.length, 0, '写值新增不得修改原对象');
assert.equal(updateProfileWrite(withWrite, 0, 'on_complete', 0, { value: false }).nodes[0].on_complete.writes[0].value, false);
assert.equal(removeProfileWrite(withWrite, 0, 'on_complete', 0).nodes[0].on_complete.writes.length, 0);

assert.equal(
  canonicalProfileJson({ ...opcProfileFixture, name: 'line-a' }),
  `${JSON.stringify(opcProfileFixture, null, 2)}\n`,
  'JSON 预览必须为稳定两空格缩进并以换行结尾',
);
assert.equal(
  isSimulatorStartAllowed({
    profile: { ...opcProfileFixture, status: 'runnable', nodes: [{ ...opcProfileFixture.nodes[0], channel: 'line-a', trigger: { all: [condition] } }] },
    localErrors: [],
    backendErrors: [],
    fileName: 'line-a.json',
    revision: 'a'.repeat(64),
    dirty: false,
    managerState: 'idle',
  }),
  true,
  '仅已保存、未修改、runnable 且 manager 空闲的 profile 可启动',
);
assert.equal(
  isSimulatorStartAllowed({
    profile: { ...opcProfileFixture, status: 'runnable' },
    localErrors: [],
    backendErrors: [],
    fileName: 'line-a.json',
    revision: 'a'.repeat(64),
    dirty: true,
    managerState: 'idle',
  }),
  false,
  '存在未保存修改时必须阻止启动',
);
assert.equal(
  isSimulatorStartAllowed({
    profile: { ...opcProfileFixture, status: 'runnable' },
    localErrors: [],
    backendErrors: [],
    fileName: 'line-a.json',
    revision: 'a'.repeat(64),
    dirty: false,
    managerState: 'failed',
    managerRestoreStatus: 'not_started',
  }),
  true,
  '模拟器进程异常退出后应允许再次启动',
);
assert.equal(
  isSimulatorStartAllowed({
    profile: { ...opcProfileFixture, status: 'runnable' },
    localErrors: [],
    backendErrors: [],
    fileName: 'line-a.json',
    revision: 'a'.repeat(64),
    dirty: false,
    managerState: 'stopped',
    managerRestoreStatus: 'error',
  }),
  true,
  '停止后 OPC 恢复失败时仍可通过确认再次启动',
);
assert.equal(
  isSimulatorStartAllowed({
    profile: { ...opcProfileFixture, status: 'runnable' },
    localErrors: [],
    backendErrors: [],
    fileName: 'line-a.json',
    revision: 'a'.repeat(64),
    dirty: false,
    managerState: 'stopping',
    managerRestoreStatus: 'pending',
  }),
  false,
  '停止进行中时必须阻止再次启动',
);
await assert.rejects(
  () => createOpcSimulatorClient(async () => new Response('{}')).generate({
    workflow: {},
    templates: [],
    scheduled_template_ids: ['task-a'],
    action_catalog: [],
    variable_catalog: [],
    name: 'line-a',
    file_name: 'line-a.json',
    opc_url: 'opc.tcp://127.0.0.1:4840',
  }),
  /无效响应/,
  'profile API 必须拒绝缺失 schema 字段的成功响应',
);
await assert.rejects(
  () => createOpcSimulatorClient(async () => new Response(JSON.stringify({
    state: 'running',
    pid: 12,
    file_name: 'line-a.json',
    revision: 'a'.repeat(64),
    opc_url: 'opc.tcp://127.0.0.1:4840',
    started_at: 1,
    ended_at: null,
    ended_monotonic: null,
    elapsed_seconds: 1,
    return_code: null,
    restore_status: 'not_started',
    last_error: null,
    recent_logs: [],
  }))).status(),
  /无效响应/,
  'status API 缺少 run_id 时必须拒绝，禁止 PID 猜测所有权',
);
let stopRequest;
const runId = 'a'.repeat(32);
const stoppedStatus = {
  state: 'stopped',
  pid: 12,
  file_name: 'line-a.json',
  revision: 'b'.repeat(64),
  run_id: runId,
  opc_url: 'opc.tcp://127.0.0.1:4840',
  started_at: 1,
  ended_at: 2,
  ended_monotonic: 2,
  elapsed_seconds: 1,
  return_code: 0,
  restore_status: 'succeeded',
  last_error: null,
  recent_logs: [],
};
assert.equal(
  opcSimulatorStopMessage(stoppedStatus),
  '模拟器已停止并完成恢复',
);
assert.match(
  opcSimulatorStopMessage({
    ...stoppedStatus,
    state: 'stopping',
    restore_status: 'pending',
  }),
  /仍在恢复/,
);
for (const unsafeStatus of [
  { ...stoppedStatus, state: 'failed', restore_status: 'error' },
  { ...stoppedStatus, state: 'failed', restore_status: 'uncertain' },
  { ...stoppedStatus, state: 'stopped', restore_status: 'error' },
]) {
  assert.match(opcSimulatorStopMessage(unsafeStatus), /阻断/);
  assert.doesNotMatch(opcSimulatorStopMessage(unsafeStatus), /完成恢复/);
}
await createOpcSimulatorClient(async (_url, init) => {
  stopRequest = init;
  return new Response(JSON.stringify(stoppedStatus));
}).stop(runId, true);
assert.equal(stopRequest.keepalive, true);
assert.equal(stopRequest.headers['content-type'], 'application/json');
assert.deepEqual(JSON.parse(stopRequest.body), { expected_run_id: runId });
const statusAbort = new AbortController();
let statusRequest;
await createOpcSimulatorClient(async (_url, init) => {
  statusRequest = init;
  return new Response(JSON.stringify(stoppedStatus));
}).status(statusAbort.signal);
assert.equal(statusRequest.signal, statusAbort.signal, 'status 请求必须使用独立 AbortSignal');
let startRequest;
await createOpcSimulatorClient(async (_url, init) => {
  startRequest = init;
  return new Response(JSON.stringify({ ...stoppedStatus, state: 'running', ended_at: null, ended_monotonic: null }));
}).start('line-a.json', 'b'.repeat(64), true);
assert.deepEqual(
  JSON.parse(startRequest.body),
  {
    file_name: 'line-a.json',
    expected_revision: 'b'.repeat(64),
    allow_unsafe_url: true,
  },
  'start API 必须仅发送固定字段和严格 URL 授权布尔值',
);
assert.match(
  mainSource,
  /opcSimulatorProfile\.opc\.url !== DEFAULT_OPC_SIMULATOR_URL[\s\S]*?window\.confirm[\s\S]*?allowUnsafeUrl/,
  '前端仅应对非默认 OPC URL 展示明确风险确认并传 true',
);

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
assert.match(
  mainSource,
  /builtWorkflow = await buildWorkflow\(\);[\s\S]*?setTaskExecutionWorkflow\(builtWorkflow\)[\s\S]*?taskApiRef\.current\.plan\([\s\S]*?false/,
  '运行调度必须先构建并固定本次 workflow，再解除排程暂停',
);
assert.match(
  mainSource,
  /runTaskExecutionCycle\([\s\S]*?window\.setInterval\(runCycle,\s*1500\)/,
  '运行调度必须立即执行完整周期并以 1.5 秒间隔继续',
);
assert.match(
  mainSource,
  /if \(!shouldRunTaskExecutionLoop \|\| \(!isTaskExecutionDraining && !taskExecutionWorkflow\)\) return;[\s\S]*?workflow:\s*\(taskExecutionWorkflow \?\? undefined\)/,
  'draining 模式不得依赖固定 workflow，normal 模式仍必须要求 workflow',
);
assert.match(
  mainSource,
  /return \(\) => window\.clearInterval\(timer\);[\s\S]*?useEffect\(\(\) => \(\) => taskExecutionControllerRef\.current\?\.pause\(\), \[\]\)/,
  'normal 切换到 draining 只能重建 timer，controller 仅在页面卸载时 abort',
);
assert.match(
  mainSource,
  /taskExecutionControllerRef\.current\?\.pause\(\)[\s\S]*?taskApiRef\.current\.plan\([\s\S]*?true/,
  '暂停必须中止当前前端请求并等待排程服务暂停',
);
assert.match(
  mainSource,
  /workspace !== 'tasks'[\s\S]*?handleTaskSchedulerPause/,
  '离开 Task workspace 时必须自动暂停执行循环',
);
assert.match(
  mainSource,
  /const \[isSchedulerTransitioning,\s*setIsSchedulerTransitioning\] = useState\(false\)[\s\S]*?taskSchedulerTransitionRef = useRef\(false\)/,
  '调度启停必须同时维护同步 ref 锁和可见 transition 状态',
);
assert.match(
  mainSource,
  /disabled=\{!taskInstances\.length \|\| isTaskWorkspaceLoading \|\| isSchedulerTransitioning\}/,
  'build/plan 转换期间必须禁用运行调度按钮',
);
const schedulerToggleSource = mainSource.match(
  /const handleTaskSchedulerToggle = useCallback\([\s\S]*?\n  \}, \[[^\n]+\]\);/,
)?.[0] || '';
assert.match(
  schedulerToggleSource,
  /setTaskExecutionWorkflow\(builtWorkflow\)[\s\S]*?taskApiRef\.current\.plan/,
  'plan 前必须固定构建成功的 execution payload',
);
assert.doesNotMatch(
  schedulerToggleSource.match(/catch \(error\) \{[\s\S]*?\n    \}/)?.[0] || '',
  /setTaskExecutionWorkflow\(null\)/,
  'plan 结果不确定时不得清空固定 payload，避免服务端 running 与前端分叉',
);
assert.deepEqual(
  toApiTrigger({
    plcDeviceId: 'custom_plc',
    variableName: 'S09 空闲',
    dataType: 'BOOL',
    value: true,
  }),
  { kind: 'opc', config: { plc_device_id: 'custom_plc', variable: 'S09 空闲', value: true } },
  'Task 条件必须映射为条件自身指定的 PLC OPC DTO',
);
assert.deepEqual(
  fromApiTrigger({
    kind: 'opc',
    config: { plc_device_id: 'custom_plc', variable: 'S09 空闲', value: true },
  }),
  {
    plcDeviceId: 'custom_plc',
    variableName: 'S09 空闲',
    dataType: 'BOOL',
    value: true,
  },
  'API trigger roundtrip 必须保留真实 PLC ID',
);
assert.throws(
  () => toApiTrigger({ variableName: 'ready', dataType: 'BOOL', value: true }),
  /缺少 PLC 设备 ID/,
  '缺少 PLC ID 时必须在发送请求前失败',
);
let missingPlcIdRequests = 0;
const missingPlcIdClient = createTaskOrchestrationClient({
  fetchImpl: async () => {
    missingPlcIdRequests += 1;
    throw new Error('不应发送请求');
  },
});
assert.throws(
  () => missingPlcIdClient.updateTemplate(
    '/tmp/demo.json',
    1,
    'task-a',
    {
      input_triggers: [
        { variableName: 'ready', dataType: 'BOOL', value: true },
      ].map(toApiTrigger),
    },
  ),
  /缺少 PLC 设备 ID/,
  '保存 DTO 构建必须在 API 调用前阻止缺失 PLC ID',
);
assert.equal(missingPlcIdRequests, 0, '缺失 PLC ID 时不得发送请求');
const taskApiRequests = [];
const taskApi = createTaskOrchestrationClient({
  baseUrl: 'http://scheduler.test/api/v1',
  uiToken: 'legacy-token',
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
        opc_snapshots: [{ plc_device_id: 'szlab_poly_plc', sequence: 2, values: { ready: true }, updated_at_by_variable: {} }],
        plc_registrations: [],
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
assert.equal(
  taskApiRequests[0].init.headers.Authorization,
  undefined,
  '本地 Task API 请求不得注入遗留 Bearer token',
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
await taskApi.deleteTemplates('/tmp/demo.json', 4, ['second', 'first']);
assert.equal(
  taskApiRequests[2].url,
  'http://scheduler.test/api/v1/templates:delete',
  '批量删除模板必须使用原子批量 API',
);
assert.deepEqual(
  JSON.parse(taskApiRequests[2].init.body),
  { workflow_path: '/tmp/demo.json', expected_version: 4, template_ids: ['second', 'first'] },
  '批量删除请求只能包含工作区、版本和模板 ID',
);
await taskApi.resetWorkspace('/tmp/demo.json');
assert.equal(
  taskApiRequests[3].url,
  'http://scheduler.test/api/v1/workspaces/reset',
  '重置当前 Task 工作区必须调用专用安全 API',
);
assert.deepEqual(
  JSON.parse(taskApiRequests[3].init.body),
  { workflow_path: '/tmp/demo.json' },
  '重置请求只能携带当前 workflow 工作区路径',
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

const cycleWorkspace = {
  version: 8,
  workspace: {
    workflow_path: '/tmp/demo.json',
    templates: [],
    task_instances: [],
    events: [],
    scheduled_template_ids: [],
    scheduler_paused: false,
    schedule_entries: [],
    opc_snapshots: [],
    plc_registrations: [],
  },
};
const cycleWorkflow = { name: 'demo', nodes: [], edges: [] };
const cycleCalls = [];
const cycleFetcher = async (url, init) => {
  cycleCalls.push({ kind: 'fetch', url, init });
  if (url === '/api/task-opc/poll') {
    return new Response(JSON.stringify({ success: true, active: true, variable_count: 2 }), { status: 200 });
  }
  return new Response(JSON.stringify({
    success: true,
    active: 1,
    in_flight: 1,
    claimed: 1,
    completed: 0,
    failed: 0,
  }), { status: 200 });
};
const cycleTaskClient = {
  advance: async (workflowPath, expectedVersion) => {
    cycleCalls.push({ kind: 'advance', workflowPath, expectedVersion });
    return { ...cycleWorkspace, version: expectedVersion + 1 };
  },
  getWorkspace: async (workflowPath) => {
    cycleCalls.push({ kind: 'getWorkspace', workflowPath });
    return cycleWorkspace;
  },
};
const cycleAbortController = new AbortController();
const cycleResult = await runTaskExecutionCycle({
  fetcher: cycleFetcher,
  taskClient: cycleTaskClient,
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
  signal: cycleAbortController.signal,
});
assert.deepEqual(
  cycleCalls.map((call) => call.kind === 'fetch' ? `${call.kind}:${call.url}` : call.kind),
  ['fetch:/api/task-opc/poll', 'advance', 'fetch:/api/task-execution/tick', 'getWorkspace'],
  '单轮必须严格执行 poll、advance、tick，再刷新最新 workspace',
);
assert.deepEqual(
  JSON.parse(cycleCalls[0].init.body),
  { task_workspace_path: '/tmp/demo.json', workflow: cycleWorkflow },
  'poll 请求必须携带 workspace 路径和 workflow',
);
assert.deepEqual(
  JSON.parse(cycleCalls[2].init.body),
  { task_workspace_path: '/tmp/demo.json', workflow: cycleWorkflow },
  'tick 请求必须携带 workspace 路径和 workflow',
);
assert.equal(cycleCalls[0].init.signal, cycleAbortController.signal, 'poll 必须透传 AbortSignal');
assert.equal(cycleCalls[2].init.signal, cycleAbortController.signal, 'tick 必须透传同一个 AbortSignal');
assert.equal(cycleCalls[1].expectedVersion, 7, 'advance 必须使用调用方提供的期望版本');
assert.deepEqual(cycleResult, {
  active: true,
  workspace: cycleWorkspace,
  tick: { active: 1, in_flight: 1, claimed: 1, completed: 0, failed: 0 },
}, '单轮应返回最终 workspace、tick 统计和 active 状态');

const inactiveCalls = [];
const inactiveResult = await runTaskExecutionCycle({
  fetcher: async (url) => {
    inactiveCalls.push(url);
    return new Response(JSON.stringify({ success: true, active: false, variable_count: 0 }), { status: 200 });
  },
  taskClient: {
    advance: async () => {
      throw new Error('inactive 时不应 advance');
    },
    getWorkspace: async () => {
      inactiveCalls.push('getWorkspace');
      return cycleWorkspace;
    },
  },
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
assert.deepEqual(inactiveCalls, ['/api/task-opc/poll', 'getWorkspace'], 'inactive 时只允许 poll 后刷新 workspace');
assert.deepEqual(inactiveResult, { active: false, workspace: cycleWorkspace, tick: null });

let missingWorkflowRequests = 0;
await assert.rejects(
  () => runTaskExecutionCycle({
    fetcher: async () => {
      missingWorkflowRequests += 1;
      throw new Error('不应发送请求');
    },
    taskClient: cycleTaskClient,
    workflowPath: '/tmp/demo.json',
    workflow: undefined,
    expectedVersion: 7,
  }),
  /缺少当前 workflow JSON/,
  '缺失 workflow 必须给出明确错误',
);
assert.equal(missingWorkflowRequests, 0, '缺失 workflow 必须在任何网络请求前失败');

await assert.rejects(
  () => runTaskExecutionCycle({
    fetcher: async () => new Response(JSON.stringify({ message: 'PLC 未连接' }), { status: 503 }),
    taskClient: cycleTaskClient,
    workflowPath: '/tmp/demo.json',
    workflow: cycleWorkflow,
    expectedVersion: 7,
  }),
  /Task OPC 采样失败.*PLC 未连接.*HTTP 503/,
  'poll 非 2xx 必须保留后端消息和 HTTP 状态',
);
await assert.rejects(
  () => runTaskExecutionCycle({
    fetcher: async () => new Response(JSON.stringify({ success: false, active: false, message: '变量读取失败' }), { status: 200 }),
    taskClient: cycleTaskClient,
    workflowPath: '/tmp/demo.json',
    workflow: cycleWorkflow,
    expectedVersion: 7,
  }),
  /Task OPC 采样失败.*变量读取失败/,
  'poll success=false 必须作为清晰错误抛出',
);

const conflictCalls = [];
const conflictResult = await runTaskExecutionCycle({
  fetcher: async (url) => {
    conflictCalls.push(url);
    return new Response(JSON.stringify(
      url === '/api/task-opc/poll'
        ? { success: true, active: true }
        : { success: true, active: 1, in_flight: 0, claimed: 1, completed: 0, failed: 0 },
    ), { status: 200 });
  },
  taskClient: {
    advance: async (_workflowPath, version) => {
      conflictCalls.push(`advance:${version}`);
      if (version === 7) throw new TaskOrchestrationBusinessError('版本冲突', 409);
      return { ...cycleWorkspace, version: version + 1 };
    },
    getWorkspace: async () => {
      conflictCalls.push('getWorkspace');
      return { ...cycleWorkspace, version: 9 };
    },
  },
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
assert.deepEqual(
  conflictCalls,
  [
    '/api/task-opc/poll',
    'advance:7',
    'getWorkspace',
    'advance:9',
    '/api/task-execution/tick',
    'getWorkspace',
  ],
  'poll 推进版本后，409 必须刷新版本并仅重试 advance 一次，成功后才 tick',
);
assert.equal(conflictResult.workspace.version, 9);

for (const retryError of [
  new TaskOrchestrationBusinessError('再次冲突', 409),
  new TaskOrchestrationBusinessError('请求非法', 422),
]) {
  let advanceCount = 0;
  let tickCalled = false;
  await assert.rejects(
    () => runTaskExecutionCycle({
      fetcher: async (url) => {
        if (url === '/api/task-execution/tick') tickCalled = true;
        return new Response(JSON.stringify({ success: true, active: true }), { status: 200 });
      },
      taskClient: {
        advance: async () => {
          advanceCount += 1;
          if (advanceCount === 1 && retryError.status === 409) {
            throw new TaskOrchestrationBusinessError('首次冲突', 409);
          }
          throw retryError;
        },
        getWorkspace: async () => ({ ...cycleWorkspace, version: 9 }),
      },
      workflowPath: '/tmp/demo.json',
      workflow: cycleWorkflow,
      expectedVersion: 7,
    }),
    (error) => error === retryError,
    '非 409 或第二次 409 必须原样抛出',
  );
  assert.equal(advanceCount, retryError.status === 409 ? 2 : 1);
  assert.equal(tickCalled, false, 'advance 最终失败时不得 tick');
}

const cancellationScenarios = [
  {
    name: 'poll 后 advance 前',
    staleAt: 2,
    conflictFirstAdvance: false,
    expectedCalls: ['/api/task-opc/poll'],
  },
  {
    name: '409 后 reload 前',
    staleAt: 3,
    conflictFirstAdvance: true,
    expectedCalls: ['/api/task-opc/poll', 'advance:7'],
  },
  {
    name: 'reload 后 retry 前',
    staleAt: 4,
    conflictFirstAdvance: true,
    expectedCalls: ['/api/task-opc/poll', 'advance:7', 'getWorkspace'],
  },
  {
    name: 'advance 后 tick 前',
    staleAt: 3,
    conflictFirstAdvance: false,
    expectedCalls: ['/api/task-opc/poll', 'advance:7'],
  },
  {
    name: 'tick 后最终 get 前',
    staleAt: 4,
    conflictFirstAdvance: false,
    expectedCalls: ['/api/task-opc/poll', 'advance:7', '/api/task-execution/tick'],
  },
];
for (const scenario of cancellationScenarios) {
  const calls = [];
  let currentCheckCount = 0;
  let advanceCount = 0;
  await assert.rejects(
    () => runTaskExecutionCycle({
      fetcher: async (url) => {
        calls.push(url);
        return new Response(JSON.stringify(
          url === '/api/task-opc/poll'
            ? { success: true, active: true }
            : { success: true, active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
        ), { status: 200 });
      },
      taskClient: {
        advance: async (_workflowPath, version) => {
          calls.push(`advance:${version}`);
          advanceCount += 1;
          if (scenario.conflictFirstAdvance && advanceCount === 1) {
            throw new TaskOrchestrationBusinessError('版本冲突', 409);
          }
          return cycleWorkspace;
        },
        getWorkspace: async () => {
          calls.push('getWorkspace');
          return { ...cycleWorkspace, version: 9 };
        },
      },
      workflowPath: '/tmp/demo.json',
      workflow: cycleWorkflow,
      expectedVersion: 7,
      isCurrent: () => {
        currentCheckCount += 1;
        return currentCheckCount !== scenario.staleAt;
      },
    }),
    (error) => error instanceof TaskExecutionCycleCancelledError,
    `${scenario.name}发现代际过期时必须抛出可识别取消错误`,
  );
  assert.deepEqual(calls, scenario.expectedCalls, `${scenario.name}取消后不得开始下一项操作`);
}

const abortedCycleController = new AbortController();
let abortedCycleAdvanced = false;
await assert.rejects(
  () => runTaskExecutionCycle({
    fetcher: async () => {
      abortedCycleController.abort();
      return new Response(JSON.stringify({ success: true, active: true }), { status: 200 });
    },
    taskClient: {
      advance: async () => {
        abortedCycleAdvanced = true;
        return cycleWorkspace;
      },
      getWorkspace: async () => cycleWorkspace,
    },
    workflowPath: '/tmp/demo.json',
    workflow: cycleWorkflow,
    expectedVersion: 7,
    signal: abortedCycleController.signal,
  }),
  (error) => error instanceof TaskExecutionCycleCancelledError,
  'poll 返回时 signal 已中止必须抛出可识别取消错误',
);
assert.equal(abortedCycleAdvanced, false, 'signal 中止后不得开始 advance');

let preStaleRequestCount = 0;
await assert.rejects(
  () => runTaskExecutionCycle({
    fetcher: async () => {
      preStaleRequestCount += 1;
      return new Response(JSON.stringify({ success: true, active: true }), { status: 200 });
    },
    taskClient: {
      advance: async () => {
        preStaleRequestCount += 1;
        return cycleWorkspace;
      },
      getWorkspace: async () => {
        preStaleRequestCount += 1;
        return cycleWorkspace;
      },
    },
    workflowPath: '/tmp/demo.json',
    workflow: cycleWorkflow,
    expectedVersion: 7,
    isCurrent: () => false,
  }),
  (error) => error instanceof TaskExecutionCycleCancelledError,
  '首个 poll 前发现过期必须抛出可识别取消错误',
);
assert.equal(preStaleRequestCount, 0, '预先过期时不得发起任何 fetch 或 task client 请求');

for (const active of [false, true]) {
  let current = true;
  const getDuringChangeCalls = [];
  await assert.rejects(
    () => runTaskExecutionCycle({
      fetcher: async (url) => {
        getDuringChangeCalls.push(url);
        return new Response(JSON.stringify(
          url === '/api/task-opc/poll'
            ? { success: true, active }
            : { success: true, active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
        ), { status: 200 });
      },
      taskClient: {
        advance: async () => {
          getDuringChangeCalls.push('advance');
          return cycleWorkspace;
        },
        getWorkspace: async () => {
          getDuringChangeCalls.push('getWorkspace');
          current = false;
          return cycleWorkspace;
        },
      },
      workflowPath: '/tmp/demo.json',
      workflow: cycleWorkflow,
      expectedVersion: 7,
      isCurrent: () => current,
    }),
    (error) => error instanceof TaskExecutionCycleCancelledError,
    `${active ? 'active' : 'inactive'} 最终 getWorkspace 期间变代必须取消结果`,
  );
  assert.equal(getDuringChangeCalls.at(-1), 'getWorkspace');
}

for (const abortAt of ['/api/task-opc/poll', '/api/task-execution/tick']) {
  const abortError = new DOMException('请求已中止', 'AbortError');
  await assert.rejects(
    () => runTaskExecutionCycle({
      fetcher: async (url) => {
        if (url === abortAt) throw abortError;
        return new Response(JSON.stringify({ success: true, active: true }), { status: 200 });
      },
      taskClient: cycleTaskClient,
      workflowPath: '/tmp/demo.json',
      workflow: cycleWorkflow,
      expectedVersion: 7,
    }),
    (error) => (
      error instanceof TaskExecutionCycleCancelledError
      && error.cause === abortError
    ),
    `${abortAt} 的 AbortError 必须统一为带 cause 的周期取消错误`,
  );
}

const nonAbortFetchError = new TypeError('网络故障');
await assert.rejects(
  () => runTaskExecutionCycle({
    fetcher: async () => { throw nonAbortFetchError; },
    taskClient: cycleTaskClient,
    workflowPath: '/tmp/demo.json',
    workflow: cycleWorkflow,
    expectedVersion: 7,
  }),
  (error) => error === nonAbortFetchError,
  '非 AbortError 的 fetch 异常必须原样抛出',
);

await assert.rejects(
  () => runTaskExecutionCycle({
    fetcher: async (url) => url === '/api/task-opc/poll'
      ? new Response(JSON.stringify({ success: true, active: true }), { status: 200 })
      : new Response(JSON.stringify({ success: false, message: '动作认领失败' }), { status: 200 }),
    taskClient: cycleTaskClient,
    workflowPath: '/tmp/demo.json',
    workflow: cycleWorkflow,
    expectedVersion: 7,
  }),
  /Task action tick 失败.*动作认领失败/,
  'tick success=false 必须作为清晰错误抛出',
);

assert.deepEqual(
  createTaskExecutionStatus(
    {
      active: 1,
      in_flight: 0,
      claimed: 2,
      completed: 1,
      failed: 0,
    },
    cycleWorkspace,
  ),
  {
    phase: 'dispatching',
    label: '采样派发',
    tick: { active: 1, in_flight: 0, claimed: 2, completed: 1, failed: 0 },
  },
  'claimed 且无在途动作时应显示采样派发及完整 tick 统计',
);
assert.equal(
  createTaskExecutionStatus(
    { active: 1, in_flight: 2, claimed: 0, completed: 0, failed: 0 },
    cycleWorkspace,
  ).phase,
  'running',
  '存在在途动作时应显示执行中',
);
const stillRunningWorkspace = {
  ...cycleWorkspace,
  workspace: {
    ...cycleWorkspace.workspace,
    task_instances: [{
      id: 'task-1',
      template_id: 'demo',
      status: 'running',
      sample_id: 'sample-1',
      order: 0,
      started_at: 1,
      finished_at: null,
    }],
  },
};
assert.equal(
  createTaskExecutionStatus(
    { active: 0, in_flight: 0, claimed: 0, completed: 3, failed: 0 },
    stillRunningWorkspace,
  ).phase,
  'idle',
  '最后 action 已收割但实例仍 running 时不得显示整体已完成',
);
const completedWorkspace = {
  ...stillRunningWorkspace,
  workspace: {
    ...stillRunningWorkspace.workspace,
    task_instances: stillRunningWorkspace.workspace.task_instances.map((instance) => ({
      ...instance,
      status: 'completed',
      finished_at: 2,
    })),
  },
};
assert.equal(
  createTaskExecutionStatus(
    { active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
    completedWorkspace,
  ).phase,
  'completed',
  '只有非空实例集合全部 completed 才能显示已完成',
);
assert.equal(
  createTaskExecutionStatus(
    { active: 0, in_flight: 0, claimed: 0, completed: 1, failed: 1 },
    completedWorkspace,
  ).phase,
  'failed',
  '失败计数必须映射为故障状态',
);
assert.equal(
  createTaskExecutionStatus(
    { active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
    {
      ...stillRunningWorkspace,
      workspace: {
        ...stillRunningWorkspace.workspace,
        pause_reason: { code: 'action_failed', detail: {} },
      },
    },
  ).phase,
  'failed',
  'workspace pause_reason 必须映射为故障状态',
);

const transitionRef = { current: false };
const transitionStates = [];
let transitionCalls = 0;
let releaseTransition;
const transitionBarrier = new Promise((resolve) => { releaseTransition = resolve; });
const firstTransition = runTaskSchedulerTransition(
  transitionRef,
  (transitioning) => transitionStates.push(transitioning),
  async () => {
    transitionCalls += 1;
    await transitionBarrier;
  },
);
assert.equal(
  await runTaskSchedulerTransition(
    transitionRef,
    (transitioning) => transitionStates.push(transitioning),
    async () => { transitionCalls += 1; },
  ),
  false,
  '同步 ref 锁必须在第一次 await 前拦截双击',
);
assert.equal(transitionCalls, 1, '双击只能执行一次 build/plan');
releaseTransition();
assert.equal(await firstTransition, true);
assert.deepEqual(transitionStates, [true, false]);

const pausedWorkspace = {
  ...cycleWorkspace,
  version: 9,
  workspace: { ...cycleWorkspace.workspace, scheduler_paused: true },
};
const reliablePauseCalls = [];
const reliablePausedWorkspace = await pauseTaskSchedulerReliably({
  taskClient: {
    plan: async (_workflowPath, version, paused) => {
      reliablePauseCalls.push(`plan:${version}:${paused}`);
      if (version === 7) throw new TaskOrchestrationBusinessError('版本冲突', 409);
      return pausedWorkspace;
    },
    getWorkspace: async () => {
      reliablePauseCalls.push('getWorkspace');
      return { ...cycleWorkspace, version: 11 };
    },
  },
  workflowPath: '/tmp/demo.json',
  expectedVersion: 7,
});
assert.equal(reliablePausedWorkspace, pausedWorkspace);
assert.deepEqual(
  reliablePauseCalls,
  ['plan:7:true', 'getWorkspace', 'plan:11:true'],
  '可靠暂停遇到 409 必须 reload 最新版本且只重试一次',
);
let reliablePauseAttempts = 0;
await assert.rejects(
  () => pauseTaskSchedulerReliably({
    taskClient: {
      plan: async () => {
        reliablePauseAttempts += 1;
        throw new TaskOrchestrationBusinessError('仍然冲突', 409);
      },
      getWorkspace: async () => ({ ...cycleWorkspace, version: 12 }),
    },
    workflowPath: '/tmp/demo.json',
    expectedVersion: 7,
  }),
  /仍然冲突/,
  '可靠暂停第二次 409 必须原样抛出',
);
assert.equal(reliablePauseAttempts, 2);

const harvestCalls = [];
const harvestResult = await runTaskExecutionHarvestCycle({
  fetcher: async (url, init) => {
    harvestCalls.push({ kind: 'fetch', url, init });
    return new Response(JSON.stringify({
      success: true,
      active: 0,
      in_flight: 1,
      claimed: 0,
      completed: 1,
      failed: 0,
    }), { status: 200 });
  },
  taskClient: {
    getWorkspace: async (workflowPath) => {
      harvestCalls.push({ kind: 'getWorkspace', workflowPath });
      return stillRunningWorkspace;
    },
  },
  workflowPath: '/tmp/demo.json',
});
assert.deepEqual(
  harvestCalls.map((call) => call.kind === 'fetch' ? call.url : call.kind),
  ['/api/task-execution/tick', 'getWorkspace'],
  'harvest-only 只能 tick 后刷新 workspace，不得 poll、advance 或 claim 新动作',
);
assert.deepEqual(JSON.parse(harvestCalls[0].init.body), {
  task_workspace_path: '/tmp/demo.json',
  harvest_only: true,
});
assert.equal(harvestResult.tick.in_flight, 1);

let releaseControlledCycle;
const controlledCycleStarted = new Promise((resolve) => {
  releaseControlledCycle = resolve;
});
let controlledCycleCalls = 0;
const controlledStatuses = [];
const controlledController = createTaskExecutionController({
  runCycle: async ({ signal }) => {
    controlledCycleCalls += 1;
    await controlledCycleStarted;
    if (signal.aborted) throw new TaskExecutionCycleCancelledError();
    return {
      active: true,
      workspace: cycleWorkspace,
      tick: { active: 1, in_flight: 1, claimed: 1, completed: 0, failed: 0 },
    };
  },
  applyWorkspace: () => {},
  pauseScheduler: async () => cycleWorkspace,
  onStatus: (status) => controlledStatuses.push(status),
  onError: (message) => { throw new Error(`不应报错：${message}`); },
});
controlledController.start();
const controlledFirst = controlledController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
assert.equal(
  await controlledController.run({
    workflowPath: '/tmp/demo.json',
    workflow: cycleWorkflow,
    expectedVersion: 7,
  }),
  false,
  '上一轮未结束时必须跳过新一轮，避免 1.5 秒定时器重入',
);
assert.equal(controlledCycleCalls, 1);
releaseControlledCycle();
assert.equal(await controlledFirst, true);
assert.equal(controlledStatuses.at(-1).phase, 'running');

const generationResolvers = [];
let generationConcurrent = 0;
let generationMaxConcurrent = 0;
let generationCycleCalls = 0;
const generationApplied = [];
const generationController = createTaskExecutionController({
  runCycle: async () => {
    generationCycleCalls += 1;
    generationConcurrent += 1;
    generationMaxConcurrent = Math.max(generationMaxConcurrent, generationConcurrent);
    const call = generationCycleCalls;
    await new Promise((resolve) => { generationResolvers[call - 1] = resolve; });
    generationConcurrent -= 1;
    return {
      active: true,
      workspace: { ...cycleWorkspace, version: 20 + call },
      tick: { active: 1, in_flight: 1, claimed: 0, completed: 0, failed: 0 },
    };
  },
  applyWorkspace: (workspace) => generationApplied.push(workspace.version),
  pauseScheduler: async () => pausedWorkspace,
  onStatus: () => {},
  onError: (message) => { throw new Error(`不应报错：${message}`); },
});
generationController.start();
const oldGenerationRun = generationController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
generationController.start();
const newGenerationRun = generationController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 8,
});
assert.equal(await generationController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 8,
}), false, '新代已有请求时必须拒绝同代重入');
generationResolvers[0]();
assert.equal(await oldGenerationRun, false, '旧代延迟结果必须静默丢弃');
assert.equal(await generationController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 8,
}), false, '旧代 finally 不得清除新代 inFlight 并触发第三并发');
assert.equal(generationCycleCalls, 2);
assert.equal(generationMaxConcurrent, 2);
assert.deepEqual(generationApplied, []);
generationResolvers[1]();
assert.equal(await newGenerationRun, true);
assert.deepEqual(generationApplied, [22], '只有当前代可以应用 workspace');

const cancellationErrors = [];
let cancellationApplied = false;
const cancellationController = createTaskExecutionController({
  runCycle: async ({ signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new TaskExecutionCycleCancelledError()), { once: true });
  }),
  applyWorkspace: () => { cancellationApplied = true; },
  pauseScheduler: async () => cycleWorkspace,
  onStatus: () => {},
  onError: (message) => cancellationErrors.push(message),
});
cancellationController.start();
const cancelledRun = cancellationController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
cancellationController.pause();
assert.equal(await cancelledRun, false, 'pause/unmount abort 应作为静默取消返回');
assert.deepEqual(cancellationErrors, []);
assert.equal(cancellationApplied, false);

const inactiveEvents = [];
let inactiveHarvestCalls = 0;
const inactiveController = createTaskExecutionController({
  runCycle: async () => ({ active: false, workspace: cycleWorkspace, tick: null }),
  runHarvestCycle: async () => {
    inactiveHarvestCalls += 1;
    return {
      active: false,
      workspace: cycleWorkspace,
      tick: { active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
    };
  },
  applyWorkspace: (workspace) => inactiveEvents.push(`apply:${workspace.version}`),
  pauseScheduler: async (workflowPath, version) => {
    inactiveEvents.push(`pause:${workflowPath}:${version}`);
    return pausedWorkspace;
  },
  onStatus: (status) => inactiveEvents.push(`status:${status.phase}`),
  onError: (message) => inactiveEvents.push(`error:${message}`),
});
inactiveController.start();
assert.equal(await inactiveController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
}), true);
assert.equal(inactiveController.isRunning(), true, 'inactive pause 后也必须至少进入一次 harvest-only');
await inactiveController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
});
assert.equal(inactiveHarvestCalls, 1);
assert.equal(inactiveController.isRunning(), false);
assert.deepEqual(inactiveEvents.slice(0, 4), [
  'status:dispatching',
  'apply:8',
  'pause:/tmp/demo.json:8',
  'apply:9',
], 'inactive 必须先应用最新 workspace，再以其版本 await pause 并应用刷新结果');

const drainEvents = [];
let harvestCount = 0;
const drainController = createTaskExecutionController({
  runCycle: async () => ({
    active: true,
    workspace: stillRunningWorkspace,
    tick: { active: 1, in_flight: 1, claimed: 1, completed: 0, failed: 0 },
  }),
  runHarvestCycle: async () => {
    harvestCount += 1;
    return {
      active: harvestCount < 2,
      workspace: harvestCount < 2 ? stillRunningWorkspace : completedWorkspace,
      tick: {
        active: 0,
        in_flight: harvestCount < 2 ? 1 : 0,
        claimed: 0,
        completed: 1,
        failed: 0,
      },
    };
  },
  applyWorkspace: (workspace) => drainEvents.push(`apply:${workspace.version}`),
  pauseScheduler: async (_workflowPath, version) => {
    drainEvents.push(`pause:${version}`);
    return {
      ...stillRunningWorkspace,
      version: version + 1,
      workspace: { ...stillRunningWorkspace.workspace, scheduler_paused: true },
    };
  },
  onStatus: (status) => drainEvents.push(`status:${status.phase}:${status.tick.in_flight}`),
  onError: (message) => drainEvents.push(`error:${message}`),
  onDrainingChange: (draining) => drainEvents.push(`draining:${draining}`),
});
drainController.start();
await drainController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
assert.equal(await drainController.pauseAndDrain({
  workflowPath: '/tmp/demo.json',
  expectedVersion: 8,
}), true, '手动暂停且仍有 in_flight 时必须进入 harvest-only');
await drainController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
});
await drainController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
});
assert.equal(harvestCount, 2);
assert.equal(drainController.isRunning(), false);
assert.deepEqual(
  drainEvents.filter((event) => event.startsWith('draining:')),
  ['draining:false', 'draining:true', 'draining:false'],
  'harvest-only 必须持续到 in_flight=0 后退出',
);
assert.equal(drainEvents.at(-1), 'status:completed:0');

const ordinaryErrorEvents = [];
const ordinaryError = new Error('设备动作认领失败');
let ordinaryErrorDiagnostic = '';
const errorController = createTaskExecutionController({
  runCycle: async () => { throw ordinaryError; },
  runHarvestCycle: async () => ({
    active: false,
    workspace: pausedWorkspace,
    tick: { active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
  }),
  applyWorkspace: () => { ordinaryErrorDiagnostic = ''; },
  pauseScheduler: async (_workflowPath, version) => {
    ordinaryErrorEvents.push(`pause:${version}`);
    return pausedWorkspace;
  },
  onStatus: (status) => ordinaryErrorEvents.push(`status:${status.phase}`),
  onError: (message) => {
    ordinaryErrorDiagnostic = message;
    ordinaryErrorEvents.push(`error:${message}`);
  },
  getLatestVersion: () => 12,
});
errorController.start();
assert.equal(await errorController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
}), false);
assert.deepEqual(
  ordinaryErrorEvents,
  ['status:dispatching', 'pause:12', 'status:failed', 'error:设备动作认领失败'],
  '普通错误应保留原始诊断、停止后续循环，并尽力暂停而不覆盖原错误',
);
assert.equal(ordinaryErrorDiagnostic, '设备动作认领失败', '暂停返回的 workspace 不得清除原始错误诊断');
assert.equal(errorController.isRunning(), true, '普通错误暂停后必须强制探测一次 harvest-only');
await errorController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 13,
});
assert.equal(errorController.isRunning(), false);

let raceBackendInFlight = 0;
let raceHarvestCalls = 0;
const raceController = createTaskExecutionController({
  runCycle: async ({ signal }) => {
    raceBackendInFlight = 1;
    return new Promise((_resolve, reject) => {
      signal.addEventListener(
        'abort',
        () => reject(new TaskExecutionCycleCancelledError()),
        { once: true },
      );
    });
  },
  runHarvestCycle: async () => {
    raceHarvestCalls += 1;
    const inFlight = raceBackendInFlight;
    raceBackendInFlight = 0;
    return {
      active: inFlight > 0,
      workspace: inFlight > 0 ? stillRunningWorkspace : completedWorkspace,
      tick: { active: 0, in_flight: inFlight, claimed: 0, completed: inFlight ? 0 : 1, failed: 0 },
    };
  },
  applyWorkspace: () => {},
  pauseScheduler: async () => pausedWorkspace,
  onStatus: () => {},
  onError: (message) => { throw new Error(`不应报错：${message}`); },
  onDrainingChange: () => {},
});
raceController.start();
const unresolvedNormalTick = raceController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
await Promise.resolve();
assert.equal(await raceController.pauseAndDrain({
  workflowPath: '/tmp/demo.json',
  expectedVersion: 8,
}), true, 'normal tick 响应未到且 lastTick=0 时暂停仍必须强制 harvest');
assert.equal(await unresolvedNormalTick, false);
await raceController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
});
assert.equal(raceController.isRunning(), true, '首次 harvest 发现 in_flight=1 时必须继续');
await raceController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
});
assert.equal(raceController.isRunning(), false);
assert.equal(raceHarvestCalls, 2);

let zeroHarvestCalls = 0;
const zeroHarvestController = createTaskExecutionController({
  runCycle: async () => { throw new Error('不应运行 normal cycle'); },
  runHarvestCycle: async () => {
    zeroHarvestCalls += 1;
    return {
      active: false,
      workspace: pausedWorkspace,
      tick: { active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
    };
  },
  applyWorkspace: () => {},
  pauseScheduler: async () => pausedWorkspace,
  onStatus: () => {},
  onError: () => {},
  onDrainingChange: () => {},
});
await zeroHarvestController.pauseAndDrain({
  workflowPath: '/tmp/demo.json',
  expectedVersion: 7,
});
await zeroHarvestController.run({
  workflowPath: '/tmp/demo.json',
  workflow: undefined,
  expectedVersion: 8,
});
await zeroHarvestController.run({
  workflowPath: '/tmp/demo.json',
  workflow: undefined,
  expectedVersion: 8,
});
assert.equal(zeroHarvestCalls, 1, '刷新后 workflow=null 的暂停仍应 harvest 一次，首次 0 后停止');

let errorDrainCycle = 0;
let errorDrainDiagnostic = '';
const errorDrainStatuses = [];
const errorDrainController = createTaskExecutionController({
  runCycle: async () => {
    errorDrainCycle += 1;
    if (errorDrainCycle === 1) {
      return {
        active: true,
        workspace: stillRunningWorkspace,
        tick: { active: 1, in_flight: 1, claimed: 1, completed: 0, failed: 0 },
      };
    }
    throw new Error('周期网络故障');
  },
  runHarvestCycle: async () => ({
    active: false,
    workspace: completedWorkspace,
    tick: { active: 0, in_flight: 0, claimed: 0, completed: 1, failed: 0 },
  }),
  applyWorkspace: () => { errorDrainDiagnostic = ''; },
  pauseScheduler: async () => ({
    ...stillRunningWorkspace,
    workspace: { ...stillRunningWorkspace.workspace, scheduler_paused: true },
  }),
  onStatus: (status) => errorDrainStatuses.push(status.phase),
  onError: (message) => { errorDrainDiagnostic = message; },
  onDrainingChange: () => {},
});
errorDrainController.start();
await errorDrainController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
await errorDrainController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 8,
});
assert.equal(errorDrainController.isRunning(), true, '错误暂停后仍有 in_flight 必须继续 harvest-only');
assert.equal(errorDrainDiagnostic, '周期网络故障');
await errorDrainController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
});
assert.equal(errorDrainDiagnostic, '周期网络故障', 'harvest workspace 刷新不得清除原始错误诊断');
assert.equal(errorDrainStatuses.at(-1), 'failed', '错误 drain 完成后仍应显示故障而非已完成');

let failedHarvestCalls = 0;
const failedHarvestDraining = [];
const failedHarvestErrors = [];
const failedHarvestController = createTaskExecutionController({
  runCycle: async () => ({
    active: true,
    workspace: stillRunningWorkspace,
    tick: { active: 1, in_flight: 1, claimed: 1, completed: 0, failed: 0 },
  }),
  runHarvestCycle: async () => {
    failedHarvestCalls += 1;
    throw new Error('harvest 通信失败');
  },
  applyWorkspace: () => {},
  pauseScheduler: async () => ({
    ...stillRunningWorkspace,
    workspace: { ...stillRunningWorkspace.workspace, scheduler_paused: true },
  }),
  onStatus: () => {},
  onError: (message) => failedHarvestErrors.push(message),
  onDrainingChange: (draining) => failedHarvestDraining.push(draining),
});
failedHarvestController.start();
await failedHarvestController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 7,
});
await failedHarvestController.pauseAndDrain({
  workflowPath: '/tmp/demo.json',
  expectedVersion: 8,
});
assert.equal(await failedHarvestController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
}), false);
assert.equal(failedHarvestController.isRunning(), false, 'harvest 自身失败必须停止当前 generation');
assert.equal(failedHarvestDraining.at(-1), false);
assert.equal(failedHarvestErrors.at(-1), 'harvest 通信失败');
assert.equal(await failedHarvestController.run({
  workflowPath: '/tmp/demo.json',
  workflow: cycleWorkflow,
  expectedVersion: 9,
}), false, 'harvest 失败后后续 interval 不得再次调用');
assert.equal(failedHarvestCalls, 1, '旧 in_flight 统计不得导致 harvest 无限重试');

const taskTemplatesFixture = [
  { id: 'solid', name: 'S07 固体加料', nodeIds: ['n1', 'n2'], resources: ['robot', 's07'], gates: ['s07'] },
  { id: 'liquid', name: 'S09 配液', nodeIds: ['n3', 'n4', 'n5'], resources: ['robot', 's09'], gates: ['s09'] },
];
assert.deepEqual(
  buildTaskGanttEntries([
    {
      instance_id: 'no-resource',
      template_id: 'template-a',
      sample_id: 'sample-a',
      start_at: 100,
      end_at: 200,
      resources: [],
      state: 'planned',
    },
  ]),
  [{
    id: 'no-resource:task:template-a',
    instanceId: 'no-resource',
    sample: 'sample-a',
    templateId: 'template-a',
    resource: 'task:template-a',
    startAt: 100,
    endAt: 200,
    state: 'planned',
  }],
  '无资源 API 甘特条目应使用通用 Task 泳道保持可见',
);
assert.deepEqual(
  buildTaskGanttEntries([
    {
      instance_id: 'legacy-resource',
      template_id: 'template-a',
      sample_id: 'sample-a',
      start_at: 100,
      end_at: 200,
      resources: ['device-a'],
      state: 'running',
    },
  ]).map((entry) => entry.resource),
  ['task:template-a'],
  'Gantt 不得把模板资源或设备投影成占用泳道',
);
assert.deepEqual(
  annotateTaskGanttEntries(
    buildTaskGanttEntries([{
      instance_id: 'with-devices',
      template_id: 'template-a',
      sample_id: 'sample-a',
      start_at: 100,
      end_at: 200,
      resources: [],
      state: 'planned',
    }]),
    [{ id: 'template-a', nodeIds: ['node-a', 'node-missing'] }],
    [{ id: 'node-a', deviceId: 'device-a' }],
  ).map(({ involvedDeviceIds, nodeInfoIncomplete, resource }) => ({
    involvedDeviceIds,
    nodeInfoIncomplete,
    resource,
  })),
  [{
    involvedDeviceIds: ['device-a'],
    nodeInfoIncomplete: true,
    resource: 'task:template-a',
  }],
  'Gantt 设备仅作为涉及设备信息，缺失节点必须明确标记不完整',
);
const creationGate = createSynchronousActionGate();
assert.equal(creationGate.tryStart(), true, '首次创建应同步获得锁');
assert.equal(creationGate.tryStart(), false, '双击第二次必须在 React state 更新前被 ref 锁拒绝');
creationGate.finish();
assert.equal(creationGate.tryStart(), true, '完整请求结束后应释放创建锁');
assert.notEqual(
  createTaskTemplateId(1, () => 'uuid-a'),
  createTaskTemplateId(2, () => 'uuid-a'),
  '模板 ID 必须通过稳定 counter 避免同毫秒冲突',
);
assert.equal(resolveTaskTemplateNameDraft('  新名称  ', '旧名称'), '新名称');
assert.equal(resolveTaskTemplateNameDraft('   ', '旧名称'), '旧名称', '空名称必须回退现有名称');
assert.equal(
  inferMethodFromTemplateNodeId('node_003_dose_powder'),
  'dose_powder',
  '模板 node_id 应能解析出 method 后缀',
);
assert.deepEqual(
  resolveTemplateNodes(
    ['node_001_pick_from_s03', 'node_003_dose_powder'],
    [
      { id: 'canvas-a', method: 'submit_pick_from_s03', deviceId: 'robot' },
      { id: 'canvas-b', method: 'dose_powder', deviceId: 's07' },
    ],
  ).map(({ templateNodeId, node }) => ({ templateNodeId, nodeId: node?.id || null })),
  [
    { templateNodeId: 'node_001_pick_from_s03', nodeId: 'canvas-a' },
    { templateNodeId: 'node_003_dose_powder', nodeId: 'canvas-b' },
  ],
  '画布节点 ID 与模板 node_id 不一致时，应回退按 method 匹配',
);
assert.deepEqual(
  taskTemplateDeviceIds(
    { nodeIds: ['node_003_dose_powder'] },
    [{ id: 'canvas-b', method: 'dose_powder', deviceId: 's07' }],
  ),
  ['s07'],
  '设备投影在 method 回退匹配后也应正确',
);
assert.deepEqual(
  updateTaskTemplateTriggers(
    updateTaskTemplateTriggers(
      [{ id: 'template-a', inputTriggers: [], outputTriggers: [] }],
      'template-a',
      'input',
      [{ variableName: 'ready', dataType: 'BOOL', value: true }],
    ),
    'template-a',
    'input',
    [],
  )[0].inputTriggers,
  [],
  '连续 trigger 增删必须基于最新本地 draft，且允许保存空数组',
);
assert.equal(isTaskWaitingStatus('waiting'), true);
assert.equal(isTaskWaitingStatus('pending'), true);
for (const terminalStatus of ['failed', 'cancelled', 'completed', 'running']) {
  assert.equal(isTaskWaitingStatus(terminalStatus), false, `${terminalStatus} 不得显示在 waiting 区`);
}
assert.equal(
  canDeleteTaskTemplate('template-a', [{ templateId: 'template-a', status: 'waiting' }], {
    schedulerBusy: false,
    actionInFlight: false,
  }),
  false,
  '存在非终态关联实例时必须禁止删除模板',
);
assert.equal(
  canDeleteTaskTemplate('template-a', [{ templateId: 'template-a', status: 'completed' }], {
    schedulerBusy: true,
    actionInFlight: false,
  }),
  false,
  '调度器运行或切换时必须禁止删除模板',
);
assert.equal(
  canDeleteTaskTemplate('template-a', [{ templateId: 'template-a', status: 'completed' }], {
    schedulerBusy: false,
    actionInFlight: false,
  }),
  true,
  '仅有终态实例且无 in-flight 操作时允许删除',
);
const workspaceEpoch = createWorkspaceEpochController();
const firstWorkspace = workspaceEpoch.begin('first.json');
const secondWorkspace = workspaceEpoch.begin('second.json');
assert.equal(firstWorkspace.signal.aborted, true, '切换路径必须中止前一路径请求');
assert.equal(workspaceEpoch.isCurrent(firstWorkspace), false, '旧路径响应不得 apply');
assert.equal(workspaceEpoch.isCurrent(secondWorkspace), true);
const operationGeneration = createOperationGenerationController();
const firstOperation = operationGeneration.begin();
const secondOperation = operationGeneration.begin();
assert.equal(operationGeneration.isCurrent(firstOperation), false, '旧操作响应不得恢复后续本地编辑');
assert.equal(operationGeneration.isCurrent(secondOperation), true);
let scheduledDraft = [];
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-a', 'add');
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-b', 'add');
assert.deepEqual(scheduledDraft, ['task-a', 'task-b'], '连续 add/add 必须保留两次最新意图');
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-a', 'remove');
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-b', 'remove');
assert.deepEqual(scheduledDraft, [], '连续 remove/remove 必须基于最新 draft');
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-a', 'add');
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-a', 'remove');
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-b', 'remove');
scheduledDraft = updateScheduledTemplateDraft(scheduledDraft, 'task-b', 'add');
assert.deepEqual(scheduledDraft, ['task-b'], 'add/remove 乱序必须以最后一次用户意图为准');
assert.deepEqual(
  createTaskTemplateDraft('task-a', '通用 Task', [
    { id: 'node-b', deviceId: 'device-b' },
    { id: 'node-a', deviceId: 'device-a', opcVariables: ['registered'] },
  ]),
  {
    id: 'task-a',
    name: '通用 Task',
    nodeIds: ['node-b', 'node-a'],
    resources: [],
    gates: [],
    inputTriggers: [],
    outputTriggers: [],
  },
  '未连接 PLC 时不应推断 Task 条件',
);
assert.deepEqual(
  createTaskTemplateDraft(
    'task-a',
    '通用 Task',
    [
      {
        id: 'node-1',
        deviceId: 'szlab_mixer_robot',
        opcVariables: [
          'S03取放料产品',
          '传感器状态_上位机[0].NO[6]',
          'S07工艺完成',
        ],
      },
      {
        id: 'node-3',
        deviceId: 'szlab_s07_solid_addition',
        opcVariables: ['S07原点信号', 'S07允许加工', 'S07工艺完成'],
      },
    ],
  ),
  {
    id: 'task-a',
    name: '通用 Task',
    nodeIds: ['node-1', 'node-3'],
    resources: [],
    gates: [],
    inputTriggers: [],
    outputTriggers: [],
  },
  'Task 模板不再维护输入/输出触发条件，统一在 OPC 模拟配置',
);
assert.deepEqual(
  taskTemplateDeviceIds(
    { nodeIds: ['node-b', 'missing', 'node-a', 'node-b'] },
    [
      { id: 'node-a', deviceId: 'device-a' },
      { id: 'node-b', deviceId: 'device-b' },
    ],
  ),
  ['device-b', 'device-a'],
  'Resource Schedule 应从模板节点动态投影去重后的真实设备泳道',
);
assert.deepEqual(
  taskTemplateDeviceIds({ nodeIds: ['missing'] }, [{ id: 'node-a', deviceId: 'device-a' }]),
  [],
  '模板节点没有设备信息时必须保留 task lane fallback',
);
assert.equal(
  taskLocalWaitingReason(
    { id: 'task-a', sample: 'sample-a', templateId: 'missing', order: 0, status: 'waiting' },
    [],
    [],
  ),
  '缺少 Task 模板',
  '等待信息应明确报告模板缺失',
);
assert.equal(
  taskLocalWaitingReason(
    { id: 'second', sample: 'sample-a', templateId: 'template-a', order: 1, status: 'waiting' },
    [
      { id: 'first', sample: 'sample-a', templateId: 'template-a', order: 0, status: 'running' },
      { id: 'other', sample: 'sample-b', templateId: 'template-a', order: 0, status: 'waiting' },
    ],
    [{ id: 'template-a' }],
  ),
  '同一样品的前序 Task 未完成',
  '等待信息只能检查同 sample 的真实前序状态',
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
  { name: 'ready', data_type: 'BOOL', initial_value: 'true', plcDeviceId: 'custom_plc' },
  { name: 'batch', data_type: 'INTEGER', initial_value: '12', plcDeviceId: 'custom_plc' },
  { name: 'temperature', data_type: 'FLOAT', initial_value: '23.5', plcDeviceId: 'custom_plc' },
  { name: 'label', data_type: 'STRING', initial_value: '样品 A', plcDeviceId: 'custom_plc' },
];
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[0]),
  { plcDeviceId: 'custom_plc', variableName: 'ready', dataType: 'BOOL', value: true },
  '布尔 CSV 变量应以初始 true 值创建条件',
);
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[1]),
  { plcDeviceId: 'custom_plc', variableName: 'batch', dataType: 'INTEGER', value: 12 },
  '整数 CSV 变量应以数字初始值创建条件',
);
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[2]),
  { plcDeviceId: 'custom_plc', variableName: 'temperature', dataType: 'FLOAT', value: 23.5 },
  '浮点 CSV 变量应以数字初始值创建条件',
);
assert.deepEqual(
  createDefaultTriggerCondition(csvVariablesFixture[3]),
  { plcDeviceId: 'custom_plc', variableName: 'label', dataType: 'STRING', value: '样品 A' },
  '字符串 CSV 变量应以文本初始值创建条件',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [{ variableName: 'ready', dataType: 'BOOL', value: false }],
    csvVariablesFixture,
  ),
  [{ plcDeviceId: 'custom_plc', variableName: 'ready', dataType: 'BOOL', value: false }],
  'legacy 条件可由当前唯一注册变量补齐真实 PLC ID',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [
      { plcDeviceId: 'plc_a', variableName: 'ready', dataType: 'BOOL', value: true },
      { plcDeviceId: 'plc_b', variableName: 'ready', dataType: 'BOOL', value: false },
    ],
    [
      { name: 'ready', data_type: 'BOOL', initial_value: 'true', plcDeviceId: 'plc_a' },
      { name: 'ready', data_type: 'BOOL', initial_value: 'false', plcDeviceId: 'plc_b' },
    ],
  ),
  [
    { plcDeviceId: 'plc_a', variableName: 'ready', dataType: 'BOOL', value: true },
    { plcDeviceId: 'plc_b', variableName: 'ready', dataType: 'BOOL', value: false },
  ],
  '多设备同名 trigger 在 edit normalize 中不得互相覆盖',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [
      { plcDeviceId: 'custom_plc', variableName: 'ready', dataType: 'BOOL', value: true },
      { plcDeviceId: 'other_plc', variableName: 'done', dataType: 'BOOL', value: false },
    ],
    [
      { name: 'ready', data_type: 'BOOL', initial_value: 'true', plcDeviceId: 'custom_plc' },
    ],
  ),
  [
    { plcDeviceId: 'custom_plc', variableName: 'ready', dataType: 'BOOL', value: true },
    { plcDeviceId: 'other_plc', variableName: 'done', dataType: 'BOOL', value: false },
  ],
  '编辑当前 PLC 条件时不得删除其他 PLC 的已有 trigger',
);
assert.deepEqual(
  normalizeTriggerConditions([], csvVariablesFixture),
  [],
  '输入或输出条件允许删除到空数组并原样保存',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [{ variableName: 'missing', dataType: 'STRING', value: '旧值' }],
    csvVariablesFixture,
  ),
  [],
  '已不存在于注册变量列表的条件不得替换为其他变量',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [{ variableName: '业务内部：样品已制备', dataType: 'BOOL', value: true }],
    csvVariablesFixture,
  ),
  [],
  '旧内部条件必须被移除，不能替换成无关 PLC 变量',
);
assert.deepEqual(
  normalizeTriggerConditions(
    [],
    [],
  ),
  [],
  '没有注册变量时不得伪造条件',
);
assert.match(
  mainSource,
  /const \[isSchedulerRunning, setIsSchedulerRunning\] = useState\(false\);/,
  'Task 编排应维护运行或暂停状态',
);
assert.match(
  mainSource,
  /isSchedulerRunning \|\| isTaskExecutionDraining \? '暂停派发' : '运行调度'/,
  'Task 编排应提供运行和暂停派发按钮',
);
assert.match(
  mainSource,
  /value=\{taskTemplateNameDraft\}[\s\S]*?onChange=\{\(event\) => setTaskTemplateNameDraft\(event\.target\.value\)\}[\s\S]*?onBlur=\{commitSelectedTaskTemplateName\}[\s\S]*?event\.key === 'Enter'/,
  '模板名称应本地编辑，并仅在 Enter 或失焦时保存',
);
assert.match(mainSource, /createTaskOrchestrationClient\(\)/, 'Task 工作区应使用独立 REST API 客户端');
assert.match(mainSource, /\/api\/task-opc\/connect/, 'Task 页面连接 OPC 必须调用 workflow_ui 后端');
assert.doesNotMatch(mainSource, /new\s+OPC|opcua|OPCUAClient/, 'Task 前端不得直接创建 OPC 客户端');
assert.doesNotMatch(mainSource, /updateSelectedTaskTriggers|task-trigger-grid|添加输入条件|添加输出条件/, 'Task 模板详情不应再提供输入/输出触发编辑器');
assert.doesNotMatch(
  `${mainSource}\n${taskOrchestrationSource}\n${taskOrchestrationApiSource}`,
  /szlab_poly_plc/,
  'Task 前端生产代码不得猜测默认 PLC ID',
);
assert.match(mainSource, /taskPlcStatus\?\.device_id \|\| '未注册 PLC'/, '无设备状态必须显示未注册 PLC');
assert.match(mainSource, /context\.plc_device_id \|\| 'PLC'/, 'waiting 缺少设备 ID 时仅显示通用 PLC');
assert.match(mainSource, /重置当前 Task 工作区/, 'sidecar 无效时应提供当前 Task 工作区重置入口');
assert.match(mainSource, /taskApiRef\.current\.resetWorkspace\(taskWorkspacePath\)/, '重置操作必须调用 Task 服务专用 API');
assert.match(
  mainSource,
  /const resetInvalidTaskWorkspace[\s\S]*?taskExecutionControllerRef\.current\?\.pause\(\)[\s\S]*?setTaskExecutionWorkflow\(null\)[\s\S]*?setTaskExecutionStatus\(createTaskExecutionStatus\(\)\)[\s\S]*?resetWorkspace\(taskWorkspacePath\)/,
  '重置 Task workspace 必须中止循环并清空旧 execution workflow/status',
);
assert.match(mainSource, /Task 编排服务不可用[\s\S]*?重试/, '服务不可用时应显示明确状态和重试按钮');
assert.match(mainSource, /taskApiRef\.current\.updateScheduledTemplates/, '待排模板变更必须通过专用 API');
assert.match(mainSource, /taskApiRef\.current\.advance/, '调度推进必须通过 API');
assert.match(mainSource, /satisfied_triggers/, '调度日志应展示启动时已经满足的输入条件');
assert.doesNotMatch(mainSource, /scheduleOneTask/, '前端不得保留本地定时完成模拟');
assert.match(mainSource, /taskRequestQueueRef\.current\.then\(execute, execute\)/, '所有 Task 写操作应串行进入单一请求队列');
assert.match(mainSource, /error instanceof TaskOrchestrationBusinessError && error\.status === 409[\s\S]*?getWorkspace\(path, epoch\.signal\)[\s\S]*?operation\(latest\.version\)/, '409 应 reload 同一路径最新版本后仅重试一次');
assert.match(
  mainSource,
  /taskExecutionControllerRef[\s\S]*?window\.setInterval\(runCycle,\s*1500\)/,
  '完整执行循环应由控制器维护取消、代际和无重入状态',
);
assert.match(
  mainSource,
  /buildTaskGanttEntries\(response\.workspace\.schedule_entries\)/,
  'API 甘特条目必须通过含通用 fallback 的泳道映射',
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
  /updateScheduledTemplateDraft\(scheduledTemplateIdsRef\.current, templateId, 'add'\)[\s\S]*?scheduledTemplateIdsRef\.current = next;[\s\S]*?updateScheduledTemplates\([\s\S]*?next/,
  '连续添加必须同步更新 ref，并将最新期望列表写入请求',
);
assert.match(
  mainSource,
  /updateScheduledTemplateDraft\(scheduledTemplateIdsRef\.current, templateId, 'remove'\)[\s\S]*?scheduledTemplateIdsRef\.current = next;[\s\S]*?updateScheduledTemplates\([\s\S]*?next/,
  '连续移除必须同步更新 ref，并将最新期望列表写入请求',
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
  /\.\.\.new Set\(taskGanttEntries\.map\(\(entry\) => entry\.resource\)\)/,
  'Task Schedule 应始终从实际 API 甘特条目生成泳道，不受待排模板开关影响',
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
assert.doesNotMatch(
  mainSource,
  /资源门控|资源条件/,
  'Task 页面文案不得暗示 scheduler 仍持有资源锁',
);
assert.match(
  mainSource,
  /按样品顺序与 Action\/OPC 握手或流程条件调度/,
  'Task Queue 应说明真实的 Action/OPC 与流程条件边界',
);
assert.match(
  styleSource,
  /\.task-schedule-resize-handle\s*\{[\s\S]*?cursor:\s*row-resize;/,
  'Resource Schedule 分隔条应使用垂直调整光标',
);
assert.match(
  mainSource,
  /生成 OPC 模拟配置[\s\S]*?disabled=\{!scheduledOpcTemplateIds\.length/,
  '未排入 Resource Schedule 时必须禁用 profile 生成入口',
);
assert.match(
  mainSource,
  /查看配置模板/,
  '应提供只读参照模板入口',
);
assert.match(
  mainSource,
  /查看生成规范/,
  '应提供 JSON 生成规范入口',
);
assert.match(
  mainSource,
  /打开配置工作台/,
  '应通过下拉选择配置文件并打开工作台',
);
assert.match(
  mainSource,
  /listProfiles/,
  '应列出 task-orchestration/configs 下的配置文件',
);
assert.match(
  opcSimulatorDialogSource,
  /配置文件/,
  '工作台应以下拉框选择配置文件',
);
assert.match(
  mainSource,
  /loadReferenceTemplate/,
  '应通过 reference/template API 载入内置示例',
);
assert.match(
  opcSimulatorDialogSource,
  /readOnly \? '配置模板（只读参照）' : '模拟配置控制台'/,
  '参照模板与工作台应区分标题',
);
assert.match(
  mainSource,
  /openOpcSimulatorWorkbench/,
  '应实现 openOpcSimulatorWorkbench 从后端载入已保存草稿',
);
assert.match(
  mainSource,
  /opcSimulatorRevision[\s\S]*?重新生成会覆盖当前工作台内容/,
  '重新生成前必须确认，避免覆盖已保存草稿',
);
assert.equal(
  defaultOpcSimulatorFileName('SZLab Robot Action Workflow'),
  'szlab-robot-action-workflow-opc-simulator.json',
  '默认 profile 文件名应来自 workflow 名称 slug',
);
assert.match(
  mainSource,
  /\/api\/opc-simulator\/profiles:generate[\s\S]*?scheduled_template_ids:\s*scheduledOpcTemplateIds/,
  '生成请求必须携带 Resource Schedule 去重保序 ID',
);
assert.match(
  mainSource,
  /const actionCatalog = buildOpcActionCatalog\(actionsRef\.current\)[\s\S]*?action_catalog: actionCatalog/,
  '生成请求必须先校验 Action device_id，再携带严格 Action OPC catalog',
);
assert.match(
  mainSource,
  /variable_catalog:\s*buildOpcVariableTypeCatalog\(csvVariablesRef\.current,\s*referencedVariableNames\)/,
  '生成请求必须携带由 CSV 严格映射的变量类型 catalog',
);
assert.match(
  mainSource,
  /csvVariablesGateRef\s*=\s*useRef\(createLatestOperationGate\(\)\)[\s\S]*?fetch\(`\/api\/csv-variables\?\$\{params\.toString\(\)\}`,\s*\{\s*signal:\s*request\.signal\s*\}\)[\s\S]*?isCurrent\(request\.generation\)[\s\S]*?setCsvVariables/,
  'CSV fetch 必须使用 AbortController generation gate，旧响应不得覆盖新 CSV',
);
assert.match(
  mainSource,
  /const variables = Array\.isArray\(payload\.variables\)[\s\S]*?opcSimulatorGenerateGateRef\.current\.invalidate\(\)[\s\S]*?csvVariablesRef\.current = variables[\s\S]*?setCsvVariables\(variables\)/,
  '切换 CSV 必须先作废并中止旧 generate，再发布新 catalog',
);
assert.match(
  mainSource,
  /const payloadActions = payload\.actions \|\| \[\][\s\S]*?opcSimulatorGenerateGateRef\.current\.invalidate\(\)[\s\S]*?actionsRef\.current = payloadActions[\s\S]*?setActions\(payloadActions\)/,
  'Action catalog 更新必须先作废旧 generate，避免旧响应覆盖',
);
assert.match(
  mainSource,
  /actionsRef\.current\s*=\s*actions;[\s\S]*?csvVariablesRef\.current\s*=\s*csvVariables;/,
  'Action 与 CSV catalog 必须同步到 latest refs',
);
assert.match(
  mainSource,
  /opcSimulatorGenerateInFlightRef\s*=\s*useRef\(false\)[\s\S]*?opcSimulatorGenerateGateRef\s*=\s*useRef\(createLatestOperationGate\(\)\)/,
  'OPC generate 必须同时维护同步锁与 latest generation gate',
);
assert.match(
  mainSource,
  /generateOpcSimulatorProfile[\s\S]*?if \(opcSimulatorGenerateInFlightRef\.current\) return;[\s\S]*?const request = opcSimulatorGenerateGateRef\.current\.begin\(\)[\s\S]*?await buildWorkflow\(\)[\s\S]*?actionsRef\.current[\s\S]*?csvVariablesRef\.current[\s\S]*?signal:\s*request\.signal[\s\S]*?isCurrent\(request\.generation\)[\s\S]*?setOpcSimulatorProfile/,
  '连续生成必须同步阻止，且 build 后使用最新 catalog 并只应用最新响应',
);
assert.match(
  mainSource,
  /csvVariablesGateRef\.current\.unmount\(\)[\s\S]*?opcSimulatorGenerateGateRef\.current\.unmount\(\)/,
  '组件卸载必须中止 CSV 与 generate 请求',
);
assert.match(
  mainSource,
  /window\.setInterval\(refreshOpcSimulatorStatus,\s*1000\)/,
  '模拟器状态必须每秒轮询',
);
assert.match(
  mainSource,
  /opcSimulatorStatusGateRef\s*=\s*useRef\(createLatestOperationGate\(\)\)/,
  'OPC status 必须维护独立 AbortController generation gate',
);
assert.match(
  mainSource,
  /const request = opcSimulatorStatusGateRef\.current\.begin\(\)[\s\S]*?status\(request\.signal\)[\s\S]*?isCurrent\(request\.generation\)/,
  '每次 poll 必须 abort 前次且仅最新 generation 可应用',
);
assert.match(
  mainSource,
  /beginOpcSimulatorControlOperation\([\s\S]*?opcSimulatorStatusGateRef\.current\.invalidate\(\)[\s\S]*?start\(/,
  'start 前必须同步阻止 poll 并使旧 generation 失效',
);
assert.match(
  mainSource,
  /const startOpcSimulator = useCallback\(async \(\) => \{\s*if \(opcSimulatorControlInFlightRef\.current\) return;/,
  'start 入口第一行必须同步拒绝重复控制操作',
);
assert.match(
  mainSource,
  /const stopOpcSimulator = useCallback\(async \(\) => \{\s*if \(opcSimulatorControlInFlightRef\.current\) return;/,
  'stop 入口第一行必须同步拒绝 start 期间 stop 与双 stop',
);
assert.match(
  mainSource,
  /opcSimulatorStatusGateRef\.current\.unmount\(\)/,
  '组件卸载必须 abort 当前 OPC status 请求',
);
assert.match(
  mainSource,
  /opcSimulatorSaveTokenRef[\s\S]*?createProfileSaveSnapshot\([\s\S]*?isProfileSaveSnapshotCurrent\(/,
  '保存必须使用 operation token 与 canonical/fileName snapshot',
);
assert.match(
  mainSource,
  /旧版本已保存，请重新保存当前修改/,
  '保存响应落后于编辑或改名时必须保留输入并提示重新保存',
);
assert.match(
  mainSource,
  /downloadText\(saved\.file_name,\s*canonicalProfileJson\(saved\.profile\)\)/,
  '下载必须直接使用后端 saved.profile 的 canonical JSON',
);
assert.match(
  mainSource,
  /ownedOpcSimulatorRunIdRef\s*=\s*useRef<string \| null>\(null\)/,
  '本标签所有权必须保存后端 run_id，不能保存 boolean 或猜 PID',
);
assert.match(
  mainSource,
  /status\.run_id !== ownedRunId[\s\S]*?!\['starting', 'running', 'stopping'\]\.includes\(status\.state\)[\s\S]*?ownedOpcSimulatorRunIdRef\.current = null/,
  '轮询发现 run_id 变化或终态时必须立即清除本标签所有权',
);
assert.match(
  mainSource,
  /latestStatus\.run_id !== ownedRunId[\s\S]*?!\['starting', 'running', 'stopping'\]\.includes\(latestStatus\.state\)[\s\S]*?JSON\.stringify\(\{ expected_run_id: ownedRunId \}\)[\s\S]*?keepalive:\s*true[\s\S]*?addEventListener\('pagehide'/,
  'pagehide 仅可停止最新状态仍匹配且活跃的本标签 run_id',
);
assert.doesNotMatch(
  mainSource,
  /handleTaskSchedulerToggle[\s\S]{0,500}startOpcSimulator|startOpcSimulator[\s\S]{0,500}handleTaskSchedulerToggle/,
  '运行调度与 OPC 模拟器启动不得互相调用',
);
assert.match(opcSimulatorDialogSource, /role="dialog"/, '配置器必须声明 dialog role');
assert.match(opcSimulatorDialogSource, /aria-modal="true"/, '配置器必须声明模态语义');
assert.match(opcSimulatorDialogSource, /event\.key === 'Escape'/, '配置器必须支持 Escape 关闭');
assert.match(
  opcSimulatorDialogSource,
  /previousActiveElementRef[\s\S]*?document\.activeElement/,
  '配置器打开时必须保存原触发元素',
);
assert.match(
  opcSimulatorDialogSource,
  /focusableElements[\s\S]*?event\.key !== 'Tab'[\s\S]*?event\.shiftKey/,
  '配置器必须将 Tab 与 Shift+Tab 焦点循环限制在 modal 内',
);
assert.match(
  opcSimulatorDialogSource,
  /getClientRects\(\)\.length > 0/,
  'focus trap 必须排除折叠区等不可见控件，避免 Tab 从可见尾项逃出 modal',
);
assert.match(
  opcSimulatorDialogSource,
  /focusables\[0\]\?\.focus\(\)[\s\S]*?dialogRef\.current\?\.focus\(\)/,
  '配置器必须优先聚焦首个可操作元素，无可聚焦项时回退到容器',
);
assert.match(
  opcSimulatorDialogSource,
  /previousActiveElement\??\.isConnected[\s\S]*?previousActiveElement\.focus\(\)/,
  '配置器关闭或卸载时必须恢复仍连接的原触发元素',
);
assert.match(
  opcSimulatorDialogSource,
  /onCloseRef\.current\(\)[\s\S]*?document\.addEventListener\('keydown', keepFocusInDialog\)[\s\S]*?}, \[\]\);/,
  '焦点生命周期只能在挂载与卸载执行，父组件重渲染不得反复抢焦点',
);
assert.match(
  opcSimulatorDialogSource,
  /opc-profile-grid[\s\S]*?opc-profile-nodes[\s\S]*?opc-profile-editor[\s\S]*?opc-profile-validation/,
  '配置器必须采用节点、结构化编辑、校验问题三栏',
);
assert.doesNotMatch(opcSimulatorDialogSource, /JSON PREVIEW|高级 JSON 编辑/, '配置器不应展示 JSON 预览或高级 JSON 编辑');
assert.equal(
  (opcSimulatorDialogSource.match(/clearProfileVariableInitialValue\(/g) || []).length >= 2,
  true,
  '方向切到 pc_to_plc 与清空按钮都必须真正删除 initial_value own property',
);
assert.doesNotMatch(
  opcSimulatorDialogSource,
  /initial_value:\s*undefined/,
  'Dialog 不得把 undefined 写入 profile',
);
assert.match(
  opcSimulatorDialogSource,
  /disabled=\{editLocked\}[\s\S]*opc-profile-editor-shell/,
  '保存中必须禁用结构化 profile 编辑区',
);
assert.match(opcSimulatorDialogSource, /保存草稿[\s\S]*?校验并保存[\s\S]*?保存并下载/, '配置器底部必须提供三种保存动作');
assert.match(
  opcSimulatorDialogSource,
  /aria-label="OPC 轮询间隔"[\s\S]*?type="number"[\s\S]*?profile\.opc\.poll_interval[\s\S]*?aria-label="OPC I\/O 超时"[\s\S]*?profile\.opc\.io_timeout/,
  '顶部必须允许编辑 schema v2 的 poll interval 与 I/O timeout 数字字段',
);
assert.match(
  opcSimulatorDialogSource,
  /validationPathsToErrors\(backendErrors,\s*profile\)/,
  '后端精确路径错误必须映射到对应节点，点击后可定位节点',
);
assert.match(
  styleSource,
  /\.opc-profile-dialog\s*\{[\s\S]*?background:[^;]*#[0-2][0-9a-f]{5}/i,
  'OPC 配置器应使用暗色工业控制室视觉',
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
  /const deleteTaskTemplate = useCallback\([\s\S]*?\n  \}, \[mutateTaskWorkspace, showCanvasToast, taskWorkspacePath\]\);/,
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
  /if \(!taskTemplateCreateGateRef\.current\.tryStart\(\)\) return;[\s\S]*?finally \{[\s\S]*?taskTemplateCreateGateRef\.current\.finish\(\)/,
  '模板创建必须使用同步 ref 锁并在完整请求结束后释放',
);
assert.match(
  mainSource,
  /createTaskTemplateId\(\+\+taskTemplateIdCounterRef\.current\)/,
  '模板 ID 必须使用稳定 counter 与随机标识生成',
);
assert.match(
  mainSource,
  /createTaskTemplateDraft\(templateId, taskName, taskNodes\)/,
  '选择节点创建模板必须直接使用通用 Task 草稿纯函数',
);
assert.match(
  mainSource,
  /node_ids: draft\.nodeIds,\s*resources: \[\],\s*input_triggers: \[\],\s*output_triggers: \[\]/,
  '创建 Task API payload 应始终提交空的输入/输出条件',
);
assert.match(
  mainSource,
  /taskMutationGenerationRef\.current\.isCurrent\(generation\)/,
  'Task 写响应必须通过操作代际检查后才能 apply',
);
assert.match(
  mainSource,
  /taskWorkspaceEpochRef\.current\.begin\(taskWorkspacePath\)[\s\S]*?getWorkspace\(taskWorkspacePath, epoch\.signal\)/,
  'workspace path 切换必须建立 epoch 并将 AbortSignal 传给加载请求',
);
assert.match(
  mainSource,
  /\.filter\(\(task\) => isTaskWaitingStatus\(task\.status\)\)/,
  'waiting 区只能展示 waiting 与 pending',
);
assert.match(
  mainSource,
  /const deleteDisabled = !canDeleteTaskTemplate[\s\S]*?disabled=\{deleteDisabled\}/,
  '模板删除按钮必须根据关联实例和 in-flight 状态禁用',
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
  /disabled=\{!selectedTaskNodes\.length \|\| isTaskTemplateCreating\}[\s\S]*?createTaskTemplateFromSelection\(\);[\s\S]*?closeCanvasContextMenu\(\);[\s\S]*?isTaskTemplateCreating \? '创建中…' : '设为 Task 模板'/,
  '右键菜单应在无选中节点或创建请求中禁用，并在操作后关闭',
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
assert.match(
  styleSource,
  /\.task-recipe-column\s*\{[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;/,
  'Task 模板栏应整体纵向滚动',
);
assert.match(
  styleSource,
  /\.task-template-list\s*\{[^}]*flex:\s*none;[^}]*overflow:\s*visible;/,
  'Task 模板列表应随整栏展开，避免 OPC 状态区挤占后无法看到模板',
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
        opc_variables: [],
      },
    },
  ],
  edges: [{ id: 'e1', source: 'load', target: 'unload' }],
});
assert.deepEqual(
  createWorkflowRequest(
    'opc-nodes',
    [{
      ...baseNodes[0],
      data: {
        ...baseNodes[0].data,
        opcVariables: ['ready', ' ready ', 'done', 'ready'],
      },
    }],
    [],
  ).nodes[0].data.opc_variables,
  ['ready', 'done'],
  'workflow payload 必须显式保留并去重节点 Action 的 opc_variables',
);
assert.throws(
  () => createWorkflowRequest(
    'invalid-opc-node',
    [{
      ...baseNodes[0],
      data: { ...baseNodes[0].data, opcVariables: ['ready', ''] },
    }],
    [],
  ),
  /opc_variables/,
  'workflow payload 必须拒绝非空字符串以外的 opc_variables',
);
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
      title: 'szlab_mixer_robot',
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
  '动作面板必须只按后端 device_id 分组，不做 robot 特殊映射',
);

assert.deepEqual(
  groupActionsByDevice([
    {
      method: 'submit_pick_from_station',
      label: '无设备搬运动作',
      description: '后端未提供设备',
    },
    {
      method: 'plain_action',
      label: '普通动作',
      description: '后端未提供设备',
    },
  ]).map((group) => ({ id: group.id, actionCount: group.actions.length })),
  [{ id: 'unknown_device', actionCount: 2 }],
  'submit_pick/place 方法名不得触发 robot 特殊分组',
);

for (const forbidden of [
  'inferTaskResources',
  'inferTaskGates',
  'inferStationKeys',
  'chunkNodesForTaskPreview',
  'SZLAB_PROCESS_TEMPLATE_SPECS',
  'createSzlabProcessTaskTemplates',
  'Robot_Home',
  'Robot_任务允许写入',
  '按流程自动切分',
  '不能保存空条件',
]) {
  assert.doesNotMatch(
    `${mainSource}\n${taskOrchestrationSource}`,
    new RegExp(forbidden),
    `Task 调度源码不得包含专用推断或硬编码：${forbidden}`,
  );
}
