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

const { buildTaskLogLines, filterTaskLogLines } = await importTypeScriptModule(
  new URL('../src/taskLogSession.ts', import.meta.url),
);
const { fetchAllTaskActionLogs, mergeTaskActionLogs } = await importTypeScriptModule(
  new URL('../src/taskActionLog.ts', import.meta.url),
);

const logLines = buildTaskLogLines({
  events: [
    { kind: 'instances_cleared', timestamp: 1000, text: 'instances_cleared' },
    { kind: 'scheduled', timestamp: 2000, text: '已派发：无额外输入条件' },
  ],
  actionEntries: [
    { seq: 4, timestamp: 2500, level: 'INFO', message: 'Action 开始', detail: {}, node_id: 'node-1' },
    { seq: 5, timestamp: 2600, level: 'INFO', message: 'Robot_Home: true，条件满足', detail: { type: 'opc_wait' }, node_id: 'node-1' },
    { seq: 6, timestamp: 2700, level: 'ERROR', message: '执行失败', detail: {}, node_id: 'node-1' },
  ],
  session: { startedAt: 2000, actionAfterSeq: 3 },
});

assert.deepEqual(logLines.map((line) => line.category), ['schedule', 'action', 'opc', 'error']);
assert.equal(filterTaskLogLines(logLines, 'all').length, 4);
assert.equal(filterTaskLogLines(logLines, 'opc').length, 1);
assert.equal(filterTaskLogLines(logLines, 'error').length, 1);

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

const schedulerSource = await readFile(new URL('../src/TaskSchedulerBench.tsx', import.meta.url), 'utf8');
assert.match(schedulerSource, /logLines: TaskLogLine\[\];/);
assert.match(schedulerSource, /const \[logFilter, setLogFilter\] = React\.useState<TaskLogCategory>\('all'\);/);
assert.match(schedulerSource, /navigator\.clipboard\?\.writeText/);
assert.match(schedulerSource, /logContainerRef\.current\.scrollTop = logContainerRef\.current\.scrollHeight/);
assert.doesNotMatch(schedulerSource, /变量历史/);

const schedulerStyles = await readFile(new URL('../src/taskSchedulerBench.css', import.meta.url), 'utf8');
assert.match(schedulerStyles, /\.scheduler-bench__state\.pending, \.scheduler-bench__state\.waiting \{ background: #edf1f3; color: #51626e; \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.running \{ background: #fff4df; color: #92510d; \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.completed \{ background: #e7f6ef; color: #198564; \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.failed \{ background: #fff0ee; color: #b54138; \}/);
assert.match(schedulerStyles, /\.scheduler-bench__state\.cancelled \{ background: repeating-linear-gradient/);
