import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

async function importTypeScriptModule(path) {
  const source = await readFile(path, 'utf8');
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2020,
      strict: true,
    },
  });
  const tempDir = await mkdtemp(join(tmpdir(), 'task-log-session-test-'));
  const tempFile = join(tempDir, 'taskLogSession.mjs');
  await writeFile(tempFile, transpiled.outputText, 'utf8');
  return import(tempFile);
}

const {
  buildTaskLogLines,
  countTaskErrorStates,
  countTaskLogCategories,
  filterTaskErrorLines,
  filterTaskLogLines,
  formatTaskLogMetadata,
  isTaskLogRecovery,
  taskLogCategoryLabel,
  taskLogPhaseLabel,
} = await importTypeScriptModule(
  new URL('../src/taskLogSession.ts', import.meta.url),
);
const { fetchAllTaskActionLogs, mergeTaskActionLogs } = await importTypeScriptModule(
  new URL('../src/taskActionLog.ts', import.meta.url),
);

const logLines = buildTaskLogLines({
  events: [
    { kind: 'instances_cleared', timestamp: 1000, text: 'instances_cleared' },
    {
      id: 'event-scheduled-1',
      kind: 'scheduled',
      timestamp: 2000,
      text: '已派发：无额外输入条件',
      instanceId: 'inst-1',
      sampleId: 'Sample A',
      templateId: 'template-1',
      detail: { satisfied_triggers: [] },
    },
  ],
  actionEntries: [
    { seq: 4, timestamp: 2500, level: 'INFO', message: 'Action 开始', detail: {}, instance_id: 'inst-1', sample_id: 'Sample A', node_id: 'node-1', execution_id: 'exec-1' },
    { seq: 5, timestamp: 2600, level: 'INFO', message: 'Robot_Home: true，条件满足', detail: { type: 'opc_wait' }, instance_id: 'inst-1', sample_id: 'Sample A', node_id: 'node-1', execution_id: 'exec-1' },
    { seq: 6, timestamp: 2700, level: 'INFO', message: '动作结果：成功', detail: { result: true }, instance_id: 'inst-1', sample_id: 'Sample A', node_id: 'node-1', execution_id: 'exec-1' },
    { seq: 7, timestamp: 2800, level: 'ERROR', message: '执行失败', detail: {}, instance_id: 'inst-1', sample_id: 'Sample A', node_id: 'node-1', execution_id: 'exec-1' },
  ],
  session: { startedAt: 2000, actionAfterSeq: 3 },
});

assert.deepEqual(logLines.map((line) => line.category), ['schedule', 'action', 'opc', 'result', 'action']);
assert.deepEqual(logLines.map((line) => line.isError), [false, false, false, false, true]);
assert.equal(filterTaskLogLines(logLines, 'all').length, 5);
assert.equal(filterTaskLogLines(logLines, 'opc').length, 1);
assert.equal(filterTaskLogLines(logLines, 'result').length, 1);
assert.equal(filterTaskLogLines(logLines, 'error').length, 1);
assert.equal(logLines[0].id, 'event:event-scheduled-1');
assert.equal(logLines[0].level, 'INFO');
assert.equal(logLines[0].instanceId, 'inst-1');
assert.equal(logLines[0].sampleId, 'Sample A');
assert.deepEqual(logLines[0].detail, { satisfied_triggers: [] });
assert.equal(logLines[1].seq, 4);
assert.equal(logLines[1].nodeId, 'node-1');
assert.equal(logLines[1].executionId, 'exec-1');

const structuredLines = buildTaskLogLines({
  events: [],
  actionEntries: [
    {
      seq: 8,
      timestamp: 2900,
      category: 'action',
      level: 'info',
      code: 'action_progress',
      phase: 'executing',
      message: '字段名包含 OPC 和失败，但这是普通进度',
      detail: {},
      instance_id: 'inst-1',
      sample_id: 'Sample A',
      template_id: 'template-1',
      node_id: 'node-1',
      execution_id: 'exec-1',
      device_id: 'device-1',
      action_name: 'move',
    },
    {
      seq: 9,
      timestamp: 3000,
      category: 'result',
      level: 'error',
      code: 'action_returned_failure',
      phase: 'completed',
      message: '动作返回 false',
      detail: { success: false },
      instance_id: 'inst-1',
      sample_id: 'Sample A',
      template_id: 'template-1',
      node_id: 'node-1',
      execution_id: 'exec-1',
      device_id: 'device-1',
      action_name: 'move',
    },
  ],
  session: { startedAt: 2000, actionAfterSeq: 7 },
});
assert.deepEqual(structuredLines.map((line) => line.category), ['action', 'result']);
assert.deepEqual(structuredLines.map((line) => line.isError), [false, true]);
assert.deepEqual(structuredLines.map((line) => line.executionCategory), ['action', 'result']);
assert.equal(structuredLines[0].code, 'action_progress');
assert.equal(structuredLines[0].phase, 'executing');
assert.equal(structuredLines[0].templateId, 'template-1');
assert.equal(structuredLines[0].deviceId, 'device-1');
assert.equal(structuredLines[0].actionName, 'move');

const lifecycleLines = buildTaskLogLines({
  events: [],
  actionEntries: [
    {
      seq: 10,
      timestamp: 3100,
      category: 'schedule',
      level: 'error',
      code: 'unsupported_action',
      phase: 'dispatching',
      message: '不支持的 Task 动作节点',
      detail: {},
      instance_id: 'inst-1',
      sample_id: 'Sample A',
      node_id: 'node-1',
      execution_id: 'exec-1',
    },
    {
      seq: 11,
      timestamp: 3200,
      category: 'schedule',
      level: 'info',
      code: 'scheduler_error_recovered',
      phase: 'recovered',
      message: '调度错误已恢复：不支持的 Task 动作节点',
      detail: { original_code: 'unsupported_action' },
      instance_id: 'inst-1',
      sample_id: 'Sample A',
      node_id: 'node-1',
      execution_id: 'exec-1',
    },
    {
      seq: 12,
      timestamp: 3300,
      category: 'opc',
      level: 'error',
      code: 'opc_poll_failed',
      phase: 'polling',
      message: 'OPC 轮询失败',
      detail: {},
      instance_id: '',
      sample_id: '',
      node_id: '',
      execution_id: '',
    },
    {
      seq: 13,
      timestamp: 3400,
      category: 'opc',
      level: 'info',
      code: 'opc_error_recovered',
      phase: 'recovered',
      message: 'OPC 轮询采样已恢复：OPC 轮询失败',
      detail: { original_code: 'opc_poll_failed' },
      instance_id: '',
      sample_id: '',
      node_id: '',
      execution_id: '',
    },
    {
      seq: 14,
      timestamp: 3500,
      category: 'opc',
      level: 'error',
      code: 'opc_poll_failed',
      phase: 'polling',
      message: 'OPC 轮询再次失败',
      detail: {},
      instance_id: '',
      sample_id: '',
      node_id: '',
      execution_id: '',
    },
  ],
  session: { startedAt: 3000, actionAfterSeq: 9 },
});
assert.deepEqual(lifecycleLines.map((line) => line.category), ['schedule', 'schedule', 'opc', 'opc', 'opc']);
assert.deepEqual(lifecycleLines.map((line) => line.isError), [true, false, true, false, true]);
assert.deepEqual(filterTaskLogLines(lifecycleLines, 'schedule').map((line) => line.seq), [10, 11]);
assert.deepEqual(filterTaskLogLines(lifecycleLines, 'opc').map((line) => line.seq), [12, 13, 14]);
assert.deepEqual(filterTaskLogLines(lifecycleLines, 'error').map((line) => line.seq), [10, 12, 14]);
assert.deepEqual(
  filterTaskLogLines(lifecycleLines, 'error').map((line) => line.errorState),
  ['recovered', 'recovered', 'active'],
);
assert.deepEqual(filterTaskErrorLines(lifecycleLines, 'active').map((line) => line.seq), [14]);
assert.deepEqual(filterTaskErrorLines(lifecycleLines, 'recovered').map((line) => line.seq), [10, 12]);
assert.deepEqual(countTaskErrorStates(lifecycleLines), { total: 3, active: 1, recovered: 2 });
assert.deepEqual(countTaskLogCategories(lifecycleLines), {
  all: 5,
  schedule: 2,
  action: 0,
  opc: 3,
  result: 0,
  error: 3,
  activeErrors: 1,
});
assert.equal(lifecycleLines[0].recoveredAt, 3200);
assert.equal(lifecycleLines[0].recoveredByLineId, 'action:11');
assert.equal(lifecycleLines[2].recoveredAt, 3400);
assert.equal(taskLogCategoryLabel(lifecycleLines[0].category), '调度');
assert.equal(taskLogPhaseLabel(lifecycleLines[0].phase), '派发');
assert.equal(isTaskLogRecovery(lifecycleLines[0]), false);
assert.equal(isTaskLogRecovery(lifecycleLines[1]), true);
assert.equal(
  formatTaskLogMetadata(lifecycleLines[0]),
  '[category=schedule] [level=error] [code=unsupported_action] [phase=dispatching] [state=recovered] [recovered_at=3200]',
);

const requestedAfterSeqs = [];
const pages = new Map([
  [0, { latest_seq: 5, next_after_seq: 2, has_more: true, entries: [{ seq: 1 }, { seq: 2 }] }],
  [2, { latest_seq: 5, next_after_seq: 4, has_more: true, entries: [{ seq: 3 }, { seq: 4 }] }],
  [4, { latest_seq: 5, next_after_seq: 5, has_more: false, entries: [{ seq: 5 }] }],
]);
const pagedLogs = await fetchAllTaskActionLogs(async (url) => {
  const afterSeq = Number(new URL(url, 'http://localhost').searchParams.get('after_seq'));
  requestedAfterSeqs.push(afterSeq);
  return {
    ok: true,
    json: async () => ({ success: true, ...pages.get(afterSeq) }),
  };
}, 'demo.json');
assert.deepEqual(requestedAfterSeqs, [0, 2, 4]);
assert.deepEqual(pagedLogs.entries.map((entry) => entry.seq), [1, 2, 3, 4, 5]);
assert.equal(pagedLogs.next_after_seq, 5);
assert.equal(pagedLogs.latest_seq, 5);

const completeFrontendHistory = mergeTaskActionLogs(
  [],
  Array.from({ length: 2105 }, (_, index) => ({ seq: index + 1 })),
);
assert.equal(completeFrontendHistory.length, 2105);
assert.equal(completeFrontendHistory[0].seq, 1);
assert.equal(completeFrontendHistory.at(-1).seq, 2105);

const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
const taskEventTextSource = mainSource.match(/function taskEventText\([\s\S]*?\nfunction taskWaitingText/)?.[0] || '';
assert.doesNotMatch(taskEventTextSource, /toLocaleTimeString/, '调度消息不得重复嵌入展示层时间戳');
assert.match(taskEventTextSource, /completed: 'Task 已完成'/, '调度事件应输出可读中文消息');
assert.match(
  mainSource,
  /const \[taskLogSession, setTaskLogSession\] = useState<TaskLogSession \| null>\(null\);/,
);
assert.match(
  mainSource,
  /setTaskLogSession\(\{\s*startedAt: Date\.now\(\),\s*actionAfterSeq: taskLogAfterSeqRef\.current,\s*\}\);[\s\S]*?const plannedWorkspace = await taskApiRef\.current\.plan\(/,
  'Task 日志会话必须在 plan/advance 前建立，避免漏掉首条调度事件',
);
assert.match(
  mainSource,
  /const \[isTaskLogBootstrapped, setIsTaskLogBootstrapped\] = useState\(false\);/,
  'Action 日志初始化完成状态必须进入 React 状态链路',
);
assert.match(
  mainSource,
  /taskLogBootstrappedRef\.current = true;\s*setIsTaskLogBootstrapped\(true\);/,
  'cursor 初始化完成后必须触发轮询 effect 重新执行',
);
assert.match(
  mainSource,
  /if \(workspace !== 'tasks' \|\| !isTaskLogBootstrapped \|\| !taskLogBootstrappedRef\.current\) return;[\s\S]*?let cancelled = false;\s*let inFlight = false;/,
  'Action 日志轮询必须等待初始化，并防止重叠请求和陈旧响应写回',
);
assert.match(
  mainSource,
  /\}, \[isTaskLogBootstrapped, taskWorkspacePath, workspace\]\);/,
  '初始化状态变化必须重新触发 Action 日志轮询',
);
assert.match(
  mainSource,
  /if \(!isTaskLogBootstrapped \|\| !taskLogBootstrappedRef\.current\) \{\s*setTaskServiceError\('Task 日志正在初始化，请稍后重试'\);\s*return;\s*\}[\s\S]*?setTaskLogSession\(/,
  'cursor 初始化完成前不得启动新的 Task 日志会话',
);
assert.match(
  mainSource,
  /const result = await fetchAllTaskActionLogs\(fetch, taskWorkspacePath,[\s\S]*?taskLogAfterSeqRef\.current = result\.next_after_seq;/,
  '轮询必须拉完所有分页，并且只按实际收到的 next_after_seq 推进 cursor',
);
assert.match(mainSource, /logError=\{taskLogError\}/, '当前 Task 日志面板必须显示日志请求错误');

const schedulerSource = await readFile(new URL('../src/TaskSchedulerBench.tsx', import.meta.url), 'utf8');
assert.match(schedulerSource, /logLines: TaskLogLine\[\];/);
assert.match(schedulerSource, /logError: string;/);
assert.match(schedulerSource, /const \[logFilter, setLogFilter\] = React\.useState<TaskLogCategory>\('all'\);/);
assert.match(schedulerSource, /const \[errorStateFilter, setErrorStateFilter\] = React\.useState<TaskErrorStateFilter>\('all'\);/);
assert.match(schedulerSource, /filterTaskLogLines\(props\.logLines, logFilter\)/);
assert.match(schedulerSource, /filterTaskErrorLines\(props\.logLines, errorStateFilter\)/);
assert.match(schedulerSource, /countTaskErrorStates\(props\.logLines\)/);
assert.match(schedulerSource, /countTaskLogCategories\(props\.logLines\)/);
assert.match(schedulerSource, /logCategoryCounts\.activeErrors/);
assert.match(schedulerSource, /未恢复/);
assert.match(schedulerSource, /\['active', '当前异常', errorStateCounts\.active\]/);
assert.match(schedulerSource, /\['recovered', '已恢复', errorStateCounts\.recovered\]/);
assert.match(schedulerSource, /formatTaskLogMetadata\(line\)/, '导出日志必须包含 code 和 phase');
assert.match(schedulerSource, /taskLogCategoryLabel\(line\.category\)/);
assert.match(schedulerSource, /taskLogPhaseLabel\(line\.phase\)/);
assert.match(schedulerSource, /isTaskLogRecovery\(line\)/);
assert.match(schedulerSource, /line\.errorState === 'active'/);
assert.match(schedulerSource, /line\.errorState === 'recovered'/);
assert.match(schedulerSource, /\['result', '结果', logCategoryCounts\.result\][\s\S]*?\['error', '错误', logCategoryCounts\.error\]/);
assert.match(schedulerSource, /function taskLogContext\(/, '日志展示必须解析样品、Task 与 Action 上下文');
assert.match(schedulerSource, /scheduler-bench__log-error/, '新版日志面板必须渲染读取错误');
assert.match(schedulerSource, /navigator\.clipboard\?\.writeText/);
assert.match(schedulerSource, /logContainerRef\.current\.scrollTop = logContainerRef\.current\.scrollHeight/);
assert.doesNotMatch(schedulerSource, /变量历史/);

const schedulerStyles = await readFile(new URL('../src/taskSchedulerBench.css', import.meta.url), 'utf8');
assert.match(schedulerStyles, /\.scheduler-bench__log-context/);
assert.match(schedulerStyles, /\.scheduler-bench__log-line\.result/);
assert.match(schedulerStyles, /\.scheduler-bench__log-meta\.code/);
assert.match(schedulerStyles, /\.scheduler-bench__log-meta\.active-error/);
assert.match(schedulerStyles, /\.scheduler-bench__log-meta\.recovered/);
assert.match(schedulerStyles, /\.scheduler-bench__error-filters/);
assert.match(schedulerStyles, /button\[data-active-errors='true'\]/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.pending,\s*\.scheduler-bench__state\.waiting \{ background: #f1f5f9; color: #475569; \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.running \{ background: var\(--amber-bg\); color: var\(--amber\); \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.completed \{ background: var\(--green-bg\); color: var\(--green\); \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.failed \{ background: var\(--red-bg\); color: var\(--red\); \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.cancelled \{\s*background: repeating-linear-gradient/);
