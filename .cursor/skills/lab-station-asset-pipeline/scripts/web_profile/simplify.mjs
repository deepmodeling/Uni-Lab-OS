// 读 job.json + job.bin，逐网格（逐面组）用 meshoptimizer 减面，写 out.json + out.bin。
import { readFileSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
const require = createRequire(process.env.MESHOPT_FROM + '/')
const { MeshoptSimplifier } = await import(pathToFileURL(require.resolve('meshoptimizer')).href)
await MeshoptSimplifier.ready
const [, , jobPath, outPath] = process.argv
const job = JSON.parse(readFileSync(jobPath + '.json', 'utf8'))
const raw = readFileSync(jobPath + '.bin')
const bin = Buffer.from(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength))
const f32 = (o, n) => new Float32Array(bin.buffer, bin.byteOffset + o, n)
const u32 = (o, n) => new Uint32Array(bin.buffer.slice(bin.byteOffset + o, bin.byteOffset + o + n * 4))
const out = []
const chunks = []
let offset = 0
for (const mesh of job.meshes) {
  const positions = f32(mesh.pos, mesh.nv * 3)
  const attrs = mesh.attrStride > 0 ? f32(mesh.attr, mesh.nv * mesh.attrStride) : null
  const scale = MeshoptSimplifier.getScale(positions, 3)
  const groups = []
  for (const group of mesh.groups) {
    const indices = u32(group.idx, group.n)
    let result = indices
    let error = 0
    if (group.n >= 3 * (job.minTriangles ?? 64)) {
      const target = job.absoluteError ? job.absoluteError / Math.max(scale, 1e-12) : job.relativeError
      const flags = ['Sparse', 'Prune', ...(process.env.MESHOPT_FLAGS ?? 'Permissive').split(',').filter(Boolean)]
      if (attrs) {
        ;[result, error] = MeshoptSimplifier.simplifyWithAttributes(indices, positions, 3, attrs, mesh.attrStride, mesh.weights, null, 0, target, flags)
      } else {
        ;[result, error] = MeshoptSimplifier.simplify(indices, positions, 3, 0, target, flags)
      }
      if (result.length === 0) result = indices
    }
    const buf = Buffer.from(result.buffer, result.byteOffset, result.byteLength)
    chunks.push(buf)
    groups.push({ idx: offset, n: result.length, error: error * scale })
    offset += buf.length
  }
  out.push({ id: mesh.id, groups })
}
writeFileSync(outPath + '.bin', Buffer.concat(chunks))
writeFileSync(outPath + '.json', JSON.stringify({ meshes: out }))
