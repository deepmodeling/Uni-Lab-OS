/**
 * dev 工作流图 → DAG 视图模型。
 *
 * 依赖有两种来源：handle 连线（`workflow_edge`，数据流）和 `execution_policy.depends_on`
 * （画布 / `@workflow` 的纯执行序）。有子节点（`parent_uuid` 指向它）的节点——循环容器、
 * 分组、组合调用——画成区域框；指向区域的依赖按调度器的平面化语义映射到成员：
 * 区域作为上游 = 它的出口成员，作为下游 = 它的入口成员（同级成员之间没有前驱 / 后继的那些）。
 */
import type { WorkflowEdge, WorkflowNode } from '../api/types'

export interface DagNode {
  uuid: string
  name: string
  /** 卡片副标题：动作名 / 节点类型。 */
  subtitle: string
  type: string
  parentUuid?: string
  /** 有子节点：画成区域框，不画卡片。 */
  region: boolean
  /** 作者顺序（图里的出现顺序），布局同层排序用。 */
  order: number
  disabled: boolean
  deviceId?: string
  /** 任务视图叠加的节点运行状态。 */
  status?: string
}

export type DagEdgeKind = 'data' | 'dependency'

export interface DagEdge {
  uuid: string
  source: string
  target: string
  kind: DagEdgeKind
}

export interface DagModel {
  nodes: DagNode[]
  edges: DagEdge[]
  warnings: string[]
}

type GraphInput = {
  nodes?: Pick<WorkflowNode, 'uuid' | 'name' | 'type' | 'parent_uuid' | 'execution_policy' | 'disabled' | 'action_name' | 'meta_data'>[]
  edges?: Pick<WorkflowEdge, 'uuid' | 'source_node_uuid' | 'target_node_uuid'>[]
}

function dependsOn(node: { execution_policy?: unknown }): string[] {
  const policy = node.execution_policy
  if (!policy || typeof policy !== 'object') return []
  const value = (policy as Record<string, unknown>).depends_on
  return Array.isArray(value) ? value.map(String).filter(Boolean) : []
}

function deviceOf(node: { meta_data?: unknown }): string | undefined {
  const meta = node.meta_data
  if (!meta || typeof meta !== 'object') return undefined
  const device = (meta as Record<string, unknown>).target_device_id
  return typeof device === 'string' && device ? device : undefined
}

export function toDagModel(graph: GraphInput, statusByNode?: Map<string, string>): DagModel {
  const source = graph.nodes ?? []
  const byUuid = new Map(source.map((node) => [node.uuid, node]))
  const children = new Map<string, string[]>()
  for (const node of source) {
    if (node.parent_uuid && byUuid.has(node.parent_uuid)) {
      children.set(node.parent_uuid, [...(children.get(node.parent_uuid) ?? []), node.uuid])
    }
  }
  const warnings: string[] = []

  const nodes: DagNode[] = source.map((node, index) => {
    const deviceId = deviceOf(node)
    const action = node.action_name ? (deviceId ? `${deviceId}.${node.action_name}` : node.action_name) : ''
    return {
      uuid: node.uuid,
      name: node.name || node.uuid,
      subtitle: action || node.type,
      type: node.type,
      parentUuid: node.parent_uuid && byUuid.has(node.parent_uuid) ? node.parent_uuid : undefined,
      region: children.has(node.uuid),
      order: index,
      disabled: Boolean(node.disabled),
      deviceId,
      status: statusByNode?.get(node.uuid),
    }
  })

  // 原始依赖（两端都是图内节点）
  type RawEdge = { uuid: string; source: string; target: string; kind: DagEdgeKind }
  const raw: RawEdge[] = []
  for (const edge of graph.edges ?? []) {
    if (!byUuid.has(edge.source_node_uuid) || !byUuid.has(edge.target_node_uuid)) {
      warnings.push(`连线 ${edge.uuid} 的端点不在图里，已跳过。`)
      continue
    }
    raw.push({ uuid: edge.uuid, source: edge.source_node_uuid, target: edge.target_node_uuid, kind: 'data' })
  }
  for (const node of source) {
    for (const upstream of dependsOn(node)) {
      if (!byUuid.has(upstream)) {
        warnings.push(`${node.name || node.uuid} 依赖的节点 ${upstream} 不在图里，已跳过。`)
        continue
      }
      raw.push({ uuid: `depends:${upstream}->${node.uuid}`, source: upstream, target: node.uuid, kind: 'dependency' })
    }
  }

  const isAncestor = (ancestor: string, uuid: string) => {
    const seen = new Set<string>()
    let parent = byUuid.get(uuid)?.parent_uuid
    while (parent && !seen.has(parent)) {
      if (parent === ancestor) return true
      seen.add(parent)
      parent = byUuid.get(parent)?.parent_uuid
    }
    return false
  }

  // 区域 → 入口 / 出口成员（递归到非区域节点）
  const expand = (uuid: string, role: 'entry' | 'exit', seen = new Set<string>()): string[] => {
    const members = children.get(uuid)
    if (!members || seen.has(uuid)) return [uuid]
    seen.add(uuid)
    const memberSet = new Set(members)
    const inner = raw.filter((edge) => memberSet.has(edge.source) && memberSet.has(edge.target))
    const boundary = members.filter((member) =>
      role === 'entry'
        ? !inner.some((edge) => edge.target === member)
        : !inner.some((edge) => edge.source === member),
    )
    return [...new Set((boundary.length ? boundary : members).flatMap((member) => expand(member, role, new Set(seen))))]
  }

  const edges: DagEdge[] = []
  const byPair = new Map<string, DagEdge>()
  for (const edge of raw) {
    if (isAncestor(edge.source, edge.target) || isAncestor(edge.target, edge.source)) continue
    for (const from of expand(edge.source, 'exit')) {
      for (const to of expand(edge.target, 'entry')) {
        if (from === to) continue
        const pair = `${from}->${to}`
        const existing = byPair.get(pair)
        if (existing) {
          if (edge.kind === 'data') existing.kind = 'data'
          continue
        }
        const mapped = edge.source === from && edge.target === to
        const value: DagEdge = { uuid: mapped ? edge.uuid : `${edge.uuid}#${pair}`, source: from, target: to, kind: edge.kind }
        byPair.set(pair, value)
        edges.push(value)
      }
    }
  }

  return { nodes, edges, warnings: [...new Set(warnings)] }
}
