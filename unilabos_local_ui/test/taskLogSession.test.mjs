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

const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
assert.match(
  mainSource,
  /const \[taskLogSession, setTaskLogSession\] = useState<TaskLogSession \| null>\(null\);/,
);
assert.match(
  mainSource,
  /const plannedWorkspace = await taskApiRef\.current\.plan\([\s\S]*?applyTaskWorkspace\(plannedWorkspace\);[\s\S]*?setTaskLogSession\(\{\s*startedAt: Date\.now\(\),\s*actionAfterSeq: taskLogAfterSeqRef\.current,\s*\}\)/,
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
