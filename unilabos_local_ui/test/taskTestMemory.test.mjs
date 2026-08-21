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
  const tempDir = await mkdtemp(join(tmpdir(), 'task-test-memory-'));
  const tempFile = join(tempDir, 'taskTestMemory.mjs');
  await writeFile(tempFile, transpiled.outputText, 'utf8');
  return import(tempFile);
}

const {
  createEmptyTaskTestMemory,
  generateSampleIds,
  loadTaskTestMemory,
  rememberedParametersForSamples,
  rememberedSampleTemplateCount,
  saveTaskTestMemory,
  taskTestMemoryKey,
  withRememberedSampleTemplateParameters,
  withoutRememberedTemplateParameters,
  withTaskSampleCount,
} = await importTypeScriptModule(new URL('../src/taskTestMemory.ts', import.meta.url));
const { orderSelectedTemplateIds } = await importTypeScriptModule(
  new URL('../src/taskOrchestration.ts', import.meta.url),
);

assert.deepEqual(generateSampleIds(5), [
  'Sample A', 'Sample B', 'Sample C', 'Sample D', 'Sample E',
]);
assert.equal(generateSampleIds(50).length, 50);
assert.deepEqual(generateSampleIds(28).slice(24), [
  'Sample Y', 'Sample Z', 'Sample AA', 'Sample AB',
]);
assert.equal(generateSampleIds(1_000).length, 999);
assert.deepEqual(generateSampleIds(1, ['Sample A']), ['Sample B']);
assert.deepEqual(
  generateSampleIds(2, ['Sample A', 'Sample B']),
  ['Sample C', 'Sample D'],
);
assert.deepEqual(generateSampleIds(2, ['Sample Z']), ['Sample AA', 'Sample AB']);
assert.deepEqual(
  generateSampleIds(1, ['自定义样品', 'Sample C']),
  ['Sample D'],
  '非标准样品名不应影响 Sample 字母序号',
);
assert.deepEqual(generateSampleIds(1, []), ['Sample A'], '清空队列后应从 Sample A 重新开始');
assert.deepEqual(
  orderSelectedTemplateIds(
    [{ id: 's03' }, { id: 's07' }, { id: 's09' }],
    ['s09', 's03', 's07'],
  ),
  ['s03', 's07', 's09'],
  '生成队列必须按左侧模板显示顺序，而不是按勾选先后顺序',
);

const values = new Map();
const storage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
};

let memory = createEmptyTaskTestMemory();
assert.equal(memory.sampleCount, 3);
memory = withTaskSampleCount(memory, 999.8);
memory = withRememberedSampleTemplateParameters(memory, 'Sample A', 's07-template', {
  's07-node': {
    powder_count: 2,
    powder_additions: [{ coarse_position: 1, target_weight: 3.5 }],
  },
});
memory = withRememberedSampleTemplateParameters(memory, 'Sample B', 's07-template', {
  's07-node': {
    powder_count: 4,
    powder_additions: [{ coarse_position: 7, target_weight: 8.5 }],
  },
});
saveTaskTestMemory(storage, 'demo.json', memory);

const restored = loadTaskTestMemory(storage, 'demo.json');
assert.equal(restored.sampleCount, 999, '样品数应持久化并限制在前端允许范围内');
assert.deepEqual(
  restored.sampleTemplateParameters,
  memory.sampleTemplateParameters,
  '每个样品的嵌套实例入参应分别恢复',
);
assert.equal(rememberedSampleTemplateCount(restored), 2, '应按样品/模板组合统计记忆');
assert.deepEqual(
  rememberedParametersForSamples(restored, ['Sample A', 'Sample B'], ['s07-template']),
  memory.sampleTemplateParameters,
  '生成队列时应返回各样品自己的模板入参',
);
assert.deepEqual(
  rememberedParametersForSamples(restored, ['Sample A'], ['other-template']),
  {},
  '未排程模板的记忆入参不应发送给后端',
);
assert.deepEqual(
  rememberedParametersForSamples(restored, ['Sample A'], ['s07-template'], {
    's07-template': { 's07-node': ['powder_count'] },
  }),
  { 'Sample A': { 's07-template': { 's07-node': { powder_count: 2 } } } },
  '旧记录应按当前模板节点和参数字段过滤',
);
assert.deepEqual(
  rememberedParametersForSamples(restored, ['Sample A'], ['s07-template'], {
    's07-template': { 'replacement-node': ['powder_count'] },
  }),
  {},
  '模板已删除的节点不应继续沿用旧入参',
);

const cleared = withoutRememberedTemplateParameters(restored);
assert.deepEqual(cleared.sampleTemplateParameters, {});
assert.equal(cleared.sampleCount, 999, '清除入参记忆不应重置样品数');

values.set('unilabos.taskTestMemory.v1.legacy.json', JSON.stringify({
  version: 1,
  sampleCount: 4,
  templateParameters: {
    's07-template': { 's07-node': { coarse_position: 4 } },
  },
}));
assert.deepEqual(
  loadTaskTestMemory(storage, 'legacy.json'),
  { ...createEmptyTaskTestMemory(), sampleCount: 4 },
  '无法判断 sample 归属的 v1 入参不得错误套用到所有样品',
);

values.set(taskTestMemoryKey('broken.json'), '{broken');
assert.deepEqual(
  loadTaskTestMemory(storage, 'broken.json'),
  createEmptyTaskTestMemory(),
  '损坏的浏览器记录应安全回退默认配置',
);

console.log('task test memory tests passed');
