#!/usr/bin/env node
/**
 * 调试控制台的 API 契约同步：后端离线导出 OpenAPI → 快照 → openapi-typescript 类型。
 *
 *   node scripts/api-sync.mjs                # 重新导出快照并生成 src/api/generated.ts
 *   node scripts/api-sync.mjs --check        # 快照与新导出、类型与快照不一致时退出码 1
 *   node scripts/api-sync.mjs --types-only   # 只按已提交快照重新生成类型（不调用 Python）
 *   node scripts/api-sync.mjs --check-types  # 只检查类型是否与已提交快照一致
 *
 * 导出调用 `python -m unilabos.server.openapi_export`，PYTHONPATH 指向仓库根目录；
 * 解释器取 UNILABOS_PYTHON，缺省依次尝试 python3 / python。比较快照时忽略
 * `info.version` 与 `info.x-openlab-protocol.unilabos_version`（只随包版本变化）。
 */
import { spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, delimiter, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = resolve(frontendDir, '..')
const snapshotPath = resolve(frontendDir, 'openapi/unilabos-openapi.json')
const typesPath = resolve(frontendDir, 'src/api/generated.ts')

const BANNER = [
  '// 由 scripts/api-sync.mjs 从 openapi/unilabos-openapi.json 生成，请勿手改。',
  '// 刷新：npm run api:sync（需要能 import unilabos 的 Python）。',
  '',
].join('\n')

function exportOpenapi() {
  const candidates = process.env.UNILABOS_PYTHON ? [process.env.UNILABOS_PYTHON] : ['python3', 'python']
  const env = {
    ...process.env,
    PYTHONPATH: [repoRoot, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
  }
  for (const python of candidates) {
    const result = spawnSync(python, ['-m', 'unilabos.server.openapi_export', '--output', '-'], {
      cwd: repoRoot,
      env,
      encoding: 'utf-8',
      maxBuffer: 64 * 1024 * 1024,
    })
    if (result.error?.code === 'ENOENT') continue
    if (result.status !== 0) {
      throw new Error(`${python} -m unilabos.server.openapi_export 失败（退出码 ${result.status}）：\n${result.stderr}`)
    }
    return result.stdout
  }
  throw new Error('找不到 Python 解释器；设置 UNILABOS_PYTHON 指向能 import unilabos 的 python')
}

function normalized(text) {
  const document = JSON.parse(text)
  if (document.info) {
    delete document.info.version
    if (document.info['x-openlab-protocol']) delete document.info['x-openlab-protocol'].unilabos_version
  }
  return JSON.stringify(document)
}

async function generateTypes(snapshotText) {
  const { default: openapiTS, astToString } = await import('openapi-typescript')
  // defaultNonNullable=false：带默认值的字段在请求体里保持可选（Pydantic 的默认值由后端补）
  const ast = await openapiTS(JSON.parse(snapshotText), { silent: true, defaultNonNullable: false })
  return BANNER + astToString(ast)
}

function readOrEmpty(path) {
  try {
    return readFileSync(path, 'utf-8')
  } catch {
    return ''
  }
}

async function main(argv) {
  const mode = argv.find((arg) => arg.startsWith('--')) ?? '--sync'
  if (mode === '--sync') {
    const snapshot = exportOpenapi()
    writeFileSync(snapshotPath, snapshot)
    writeFileSync(typesPath, await generateTypes(snapshot))
    console.log(`已写入 ${snapshotPath}\n已写入 ${typesPath}`)
    return 0
  }
  if (mode === '--types-only') {
    writeFileSync(typesPath, await generateTypes(readFileSync(snapshotPath, 'utf-8')))
    console.log(`已写入 ${typesPath}`)
    return 0
  }
  if (mode !== '--check' && mode !== '--check-types') {
    console.error(`未知参数 ${mode}`)
    return 2
  }
  const problems = []
  const committed = readOrEmpty(snapshotPath)
  if (mode === '--check') {
    if (!committed || normalized(committed) !== normalized(exportOpenapi())) {
      problems.push('openapi/unilabos-openapi.json 与后端当前导出不一致')
    }
  }
  if (!committed) {
    problems.push('缺少 openapi/unilabos-openapi.json')
  } else if (readOrEmpty(typesPath) !== (await generateTypes(committed))) {
    problems.push('src/api/generated.ts 与快照不一致')
  }
  if (problems.length) {
    console.error(`${problems.join('\n')}\n运行 npm run api:sync 后提交两份产物。`)
    return 1
  }
  console.log('API 快照与生成类型均为最新')
  return 0
}

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (error) => {
    console.error(error instanceof Error ? error.message : error)
    process.exit(1)
  },
)
