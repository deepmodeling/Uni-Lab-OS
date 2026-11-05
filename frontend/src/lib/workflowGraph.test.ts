import { describe, expect, it } from 'vitest'
import type { WorkflowGraph } from '../api/types'
import complexDemo from '../test/fixtures/graph-complex-demo.json'
import nestedLoops from '../test/fixtures/graph-nested-loops.json'
import { toDagModel, type DagModel } from './workflowGraph'

const named = (model: DagModel) => {
  const names = new Map(model.nodes.map((node) => [node.uuid, node.name]))
  return model.edges.map((edge) => `${names.get(edge.source)} -> ${names.get(edge.target)}`)
}

describe('toDagModel on real @workflow graphs (canvas style: depends_on, no edges)', () => {
  it('turns loops with a body into regions and maps their dependencies to entries / exits', () => {
    const model = toDagModel(complexDemo as unknown as WorkflowGraph)
    expect(model.nodes.filter((node) => node.region).map((node) => node.name)).toEqual(['加样 ×2', '提纯直到达标'])
    // 空循环体的 while（等待升温）本身就是可执行节点，不是区域
    expect(model.nodes.find((node) => node.name === '等待升温')?.region).toBe(false)
    expect(named(model)).toEqual([
      '复位 -> 加样',
      '加样 -> 开始升温',
      '开始升温 -> 等待升温',
      '等待升温 -> 停止升温',
      '停止升温 -> 提纯',
      '提纯 -> 取样检测',
      '取样检测 -> 汇总报告',
    ])
    expect(model.edges.every((edge) => edge.kind === 'dependency')).toBe(true)
    expect(model.warnings).toEqual([])
    expect(model.nodes.find((node) => node.name === '复位')?.subtitle).toBe('reactor.reset')
  })

  it('expands nested regions recursively', () => {
    const model = toDagModel(nestedLoops as unknown as WorkflowGraph)
    expect(named(model)).toEqual(['复位 -> 切换到板', '切换到板 -> 孔位加样', '孔位加样 -> 汇总报告'])
    const inner = model.nodes.find((node) => node.name === '板孔 ×3')
    expect(inner?.region).toBe(true)
    expect(model.nodes.find((node) => node.uuid === inner?.parentUuid)?.name).toBe('板 ×2')
  })
})

describe('toDagModel with handle edges', () => {
  const node = (uuid: string, extra: Record<string, unknown> = {}) => ({
    uuid,
    name: uuid.toUpperCase(),
    type: 'device_action',
    execution_policy: {},
    disabled: false,
    meta_data: {},
    ...extra,
  })

  it('merges a data edge and a depends_on between the same pair, preferring data', () => {
    const model = toDagModel({
      nodes: [node('a'), node('b', { execution_policy: { depends_on: ['a', 'missing'] } })],
      edges: [
        { uuid: 'e1', source_node_uuid: 'a', target_node_uuid: 'b' },
        { uuid: 'e2', source_node_uuid: 'a', target_node_uuid: 'gone' },
      ],
    })
    expect(model.edges).toEqual([{ uuid: 'e1', source: 'a', target: 'b', kind: 'data' }])
    expect(model.warnings).toHaveLength(2)
  })

  it('overlays node run status and drops edges between a region and its own members', () => {
    const model = toDagModel(
      {
        nodes: [node('g', { type: 'group' }), node('a', { parent_uuid: 'g', execution_policy: { depends_on: ['g'] } })],
        edges: [],
      },
      new Map([['a', 'running']]),
    )
    expect(model.edges).toEqual([])
    expect(model.nodes.find((item) => item.uuid === 'a')?.status).toBe('running')
  })
})
