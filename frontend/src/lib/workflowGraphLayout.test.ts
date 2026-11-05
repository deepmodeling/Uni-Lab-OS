import { describe, expect, it } from 'vitest'
import type { WorkflowGraph } from '../api/types'
import nestedLoops from '../test/fixtures/graph-nested-loops.json'
import type { DagEdge, DagNode } from './workflowGraph'
import { toDagModel } from './workflowGraph'
import { applyNodeOffsets, layoutDag } from './workflowGraphLayout'

const node = (uuid: string, order: number, extra: Partial<DagNode> = {}): DagNode => ({
  uuid,
  name: uuid,
  subtitle: '',
  type: 'device_action',
  region: false,
  order,
  disabled: false,
  ...extra,
})
const edge = (source: string, target: string): DagEdge => ({ uuid: `${source}-${target}`, source, target, kind: 'dependency' })

describe('layoutDag', () => {
  it('ranks by longest path and orders siblings by author order', () => {
    const layout = layoutDag(
      [node('a', 0), node('c', 2), node('b', 1), node('d', 3)],
      [edge('a', 'b'), edge('a', 'c'), edge('b', 'd'), edge('c', 'd')],
      { ranksPerBand: 4 },
    )
    const byId = new Map(layout.nodes.map((positioned) => [positioned.node.uuid, positioned]))
    expect(['a', 'b', 'c', 'd'].map((id) => byId.get(id)!.rank)).toEqual([0, 1, 1, 2])
    expect(byId.get('b')!.y).toBeLessThan(byId.get('c')!.y)
    expect(byId.get('b')!.x).toBe(byId.get('c')!.x)
    expect(layout.edges).toHaveLength(4)
    expect(layout.warnings).toEqual([])
  })

  it('folds into snake bands: the second band runs right to left', () => {
    const nodes = ['a', 'b', 'c', 'd', 'e'].map((id, index) => node(id, index))
    const layout = layoutDag(nodes, [edge('a', 'b'), edge('b', 'c'), edge('c', 'd'), edge('d', 'e')], { ranksPerBand: 2 })
    const byId = new Map(layout.nodes.map((positioned) => [positioned.node.uuid, positioned]))
    expect(['a', 'b', 'c', 'd', 'e'].map((id) => byId.get(id)!.band)).toEqual([0, 0, 1, 1, 2])
    expect(byId.get('c')!.x).toBeGreaterThan(byId.get('d')!.x)
    expect(byId.get('e')!.x).toBe(byId.get('a')!.x)
    // 跨行的连线从下边缘出发
    const cross = layout.edges.find((positioned) => positioned.edge.uuid === 'b-c')!
    expect(cross.path.startsWith(`M ${byId.get('b')!.x + byId.get('b')!.width / 2} ${byId.get('b')!.y + byId.get('b')!.height}`)).toBe(true)
  })

  it('puts a cycle in the last rank with a warning instead of looping forever', () => {
    const layout = layoutDag([node('a', 0), node('b', 1), node('c', 2)], [edge('a', 'b'), edge('b', 'c'), edge('c', 'b')])
    expect(layout.warnings[0]).toMatch(/循环依赖/)
    const ranks = Object.fromEntries(layout.nodes.map((positioned) => [positioned.node.uuid, positioned.rank]))
    expect(ranks).toEqual({ a: 0, b: 1, c: 1 })
  })

  it('frames regions around their members and nests outer frames around inner ones', () => {
    const model = toDagModel(nestedLoops as unknown as WorkflowGraph)
    const layout = layoutDag(model.nodes, model.edges, { ranksPerBand: 4 })
    const group = (name: string) => layout.groups.find((item) => item.node.name === name)!
    const outer = group('板 ×2')
    const inner = group('板孔 ×3')
    expect(outer.depth).toBe(1)
    expect(inner.depth).toBe(0)
    expect(outer.children.map((child) => child.node.name).sort()).toEqual(['切换到板', '孔位加样'])
    for (const frame of [...outer.frames, ...inner.frames]) {
      expect(frame.y).toBeGreaterThanOrEqual(0)
      expect(frame.x).toBeGreaterThanOrEqual(0)
    }
    const innerFrame = inner.frames[0]
    const outerFrame = outer.frames.find((frame) => frame.x <= innerFrame.x && frame.x + frame.width >= innerFrame.x + innerFrame.width)!
    expect(outerFrame.y).toBeLessThan(innerFrame.y)
    expect(outerFrame.y + outerFrame.height).toBeGreaterThan(innerFrame.y + innerFrame.height)
    // 每个成员都在自己区域的某个框里
    for (const child of inner.children) {
      expect(
        inner.frames.some((frame) => child.x >= frame.x && child.x + child.width <= frame.x + frame.width && child.y >= frame.y && child.y + child.height <= frame.y + frame.height),
      ).toBe(true)
    }
  })

  it('applies drag offsets to nodes, frames and edge paths', () => {
    const layout = layoutDag([node('a', 0), node('b', 1)], [edge('a', 'b')])
    const moved = applyNodeOffsets(layout, { b: { x: 10, y: 40 } })
    const before = layout.nodes.find((positioned) => positioned.node.uuid === 'b')!
    const after = moved.nodes.find((positioned) => positioned.node.uuid === 'b')!
    expect([after.x - before.x, after.y - before.y]).toEqual([10, 40])
    expect(moved.edges[0].path).not.toBe(layout.edges[0].path)
  })
})
