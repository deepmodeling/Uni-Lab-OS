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
    },
  });
  const tempDir = await mkdtemp(join(tmpdir(), 'task-polling-test-'));
  const tempFile = join(tempDir, 'taskPolling.mjs');
  await writeFile(tempFile, transpiled.outputText, 'utf8');
  return import(tempFile);
}

const { TASK_EXECUTION_POLL_INTERVAL_MS } = await importTypeScriptModule(
  new URL('../src/taskPolling.ts', import.meta.url),
);

assert.equal(
  TASK_EXECUTION_POLL_INTERVAL_MS,
  5000,
  'Task 执行循环最多每 5 秒请求一次 tick',
);

const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
assert.match(
  mainSource,
  /const plannedWorkspace = await taskApiRef\.current\.plan\([\s\S]*?const advancedWorkspace = await taskApiRef\.current\.advance\(\s*taskWorkspacePath,\s*plannedWorkspace\.version,\s*\);[\s\S]*?applyTaskWorkspace\(advancedWorkspace\);/,
  '开始派发后应立即推进第一个可执行 Task，不能只恢复排程状态',
);
