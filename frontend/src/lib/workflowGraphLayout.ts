/**
 * 蛇形分层 DAG 布局（移植自 v1.0 frontend/src/lib/workflowGraphLayout.ts）。
 *
 * 拓扑分层（最长路径）→ 层内按前驱重心排序 → 每 `ranksPerBand` 层折一行、奇数行反向；
 * 区域节点不参与分层，按成员包框（同一行里不连续的成员拆成多个框）。
 */
import type { DagEdge, DagNode } from './workflowGraph'

const NODE_WIDTH = 176
const NODE_HEIGHT = 78
const GROUP_WIDTH = 200
const GROUP_FRAME_SIDE = 12
const GROUP_FRAME_TOP = 30
const GROUP_FRAME_BOTTOM = 12
const COLUMN_GAP = 76
const ROW_GAP = 46
const BAND_GAP = 88
const STAGE_PADDING = 36
/** 嵌套区域每多一层，外框向外让出的边距。 */
const NEST_SIDE = 10
const NEST_TOP = 26
const NEST_BOTTOM = 10

export const LAYOUT_COLUMN_WIDTH = GROUP_WIDTH + COLUMN_GAP

interface LayoutOptions {
  ranksPerBand?: number
}

export type GroupFrame = { x: number; y: number; width: number; height: number }

export interface PositionedNode {
  node: DagNode
  x: number
  y: number
  width: number
  height: number
  rank: number
  band: number
  /** 最近的区域祖先。 */
  groupUuid?: string
  /** 全部区域祖先（由近到远）。 */
  groupUuids: string[]
}

export interface PositionedGroup {
  node: DagNode
  /** 内部还嵌套几层区域（0 = 只有普通成员）；外框据此外扩，避免与内框重叠。 */
  depth: number
  rank: number
  band: number
  children: PositionedNode[]
  frames: GroupFrame[]
}

export interface PositionedEdge {
  edge: DagEdge
  source: PositionedNode
  target: PositionedNode
  path: string
}

export interface DagLayout {
  width: number
  height: number
  nodes: PositionedNode[]
  groups: PositionedGroup[]
  edges: PositionedEdge[]
  warnings: string[]
  ranksPerBand: number
}

interface LayoutEntity {
  id: string
  node: DagNode
  rank: number
  band: number
  x: number
  y: number
  width: number
  height: number
}

function compareNodes(left: DagNode, right: DagNode) {
  return left.order - right.order || left.uuid.localeCompare(right.uuid)
}

function compareEntities(left: LayoutEntity, right: LayoutEntity) {
  return compareNodes(left.node, right.node)
}

function frameForNodes(nodes: PositionedNode[]): GroupFrame {
  const minX = Math.min(...nodes.map((node) => node.x))
  const minY = Math.min(...nodes.map((node) => node.y))
  const maxX = Math.max(...nodes.map((node) => node.x + node.width))
  const maxY = Math.max(...nodes.map((node) => node.y + node.height))
  return {
    x: minX - GROUP_FRAME_SIDE,
    y: minY - GROUP_FRAME_TOP,
    width: maxX - minX + GROUP_FRAME_SIDE * 2,
    height: maxY - minY + GROUP_FRAME_TOP + GROUP_FRAME_BOTTOM,
  }
}

function frameContainsNode(frame: GroupFrame, node: PositionedNode) {
  const centerX = node.x + node.width / 2
  const centerY = node.y + node.height / 2
  return centerX >= frame.x && centerX <= frame.x + frame.width && centerY >= frame.y && centerY <= frame.y + frame.height
}

function inflate(frame: GroupFrame, depth: number): GroupFrame {
  return {
    x: frame.x - depth * NEST_SIDE,
    y: frame.y - depth * NEST_TOP,
    width: frame.width + depth * NEST_SIDE * 2,
    height: frame.height + depth * (NEST_TOP + NEST_BOTTOM),
  }
}

/** 同一行里连续、且框内不夹带无关节点的成员合成一个框。 */
export function groupFrames(children: PositionedNode[], positionedNodes: PositionedNode[], depth = 0): GroupFrame[] {
  const childrenByBand = new Map<number, PositionedNode[]>()
  children.forEach((child) => childrenByBand.set(child.band, [...(childrenByBand.get(child.band) ?? []), child]))
  const memberUuids = new Set(children.map((child) => child.node.uuid))
  return [...childrenByBand.entries()]
    .sort(([left], [right]) => left - right)
    .flatMap(([, values]) => {
      const sorted = [...values].sort(
        (left, right) => left.rank - right.rank || left.y - right.y || left.x - right.x || compareNodes(left.node, right.node),
      )
      const runs: PositionedNode[][] = []
      sorted.forEach((child) => {
        const current = runs.at(-1)
        if (!current) {
          runs.push([child])
          return
        }
        const maxRank = Math.max(...current.map((node) => node.rank))
        const candidateFrame = frameForNodes([...current, child])
        const containsUnrelatedNode = positionedNodes.some(
          (node) => node.band === child.band && !memberUuids.has(node.node.uuid) && frameContainsNode(candidateFrame, node),
        )
        if (child.rank <= maxRank + 1 && !containsUnrelatedNode) current.push(child)
        else runs.push([child])
      })
      return runs.map((run) => inflate(frameForNodes(run), depth))
    })
}

export function edgePath(source: PositionedNode, target: PositionedNode) {
  if (target.band > source.band) {
    const sourceX = source.x + source.width / 2
    const sourceY = source.y + source.height
    const targetX = target.x + target.width / 2
    const targetY = target.y
    const middleY = sourceY + Math.max(42, (targetY - sourceY) / 2)
    return `M ${sourceX} ${sourceY} C ${sourceX} ${middleY}, ${targetX} ${middleY}, ${targetX} ${targetY}`
  }
  const travelsRight = target.x >= source.x
  const sourceX = travelsRight ? source.x + source.width : source.x
  const targetX = travelsRight ? target.x : target.x + target.width
  const sourceY = source.y + source.height / 2
  const targetY = target.y + target.height / 2
  const bend = Math.max(42, Math.abs(targetX - sourceX) * 0.42)
  const sourceControlX = sourceX + (travelsRight ? bend : -bend)
  const targetControlX = targetX + (travelsRight ? -bend : bend)
  return `M ${sourceX} ${sourceY} C ${sourceControlX} ${sourceY}, ${targetControlX} ${targetY}, ${targetX} ${targetY}`
}

export function layoutDag(nodes: DagNode[], edges: DagEdge[], options: LayoutOptions = {}): DagLayout {
  const ranksPerBand = Math.max(2, Math.floor(options.ranksPerBand || 4))
  const warnings: string[] = []
  const nodesByUuid = new Map(nodes.map((node) => [node.uuid, node]))
  const groupNodes = nodes.filter((node) => node.region).sort(compareNodes)
  const groupIds = new Set(groupNodes.map((node) => node.uuid))
  const depthMemo = new Map<string, number>()
  const groupDepth = (uuid: string, seen = new Set<string>()): number => {
    if (depthMemo.has(uuid)) return depthMemo.get(uuid)!
    if (seen.has(uuid)) return 0
    seen.add(uuid)
    const inner = groupNodes.filter((group) => group.parentUuid === uuid)
    const depth = inner.length ? 1 + Math.max(...inner.map((group) => groupDepth(group.uuid, seen))) : 0
    depthMemo.set(uuid, depth)
    return depth
  }
  const maxDepth = groupNodes.length ? 1 + Math.max(...groupNodes.map((group) => groupDepth(group.uuid))) : 0
  const entities: LayoutEntity[] = nodes
    .filter((node) => !node.region)
    .map((node) => ({ id: node.uuid, node, rank: 0, band: 0, x: 0, y: 0, width: NODE_WIDTH, height: NODE_HEIGHT }))
    .sort(compareEntities)
  const entitiesById = new Map(entities.map((entity) => [entity.id, entity]))

  const adjacency = new Map<string, Set<string>>()
  const predecessors = new Map<string, Set<string>>()
  const indegree = new Map(entities.map((entity) => [entity.id, 0]))
  edges.forEach((edge) => {
    const source = entitiesById.get(edge.source)
    const target = entitiesById.get(edge.target)
    if (!source || !target) {
      warnings.push(
        nodesByUuid.has(edge.source) && nodesByUuid.has(edge.target)
          ? `连线 ${edge.uuid} 指向区域节点，未参与布局。`
          : `连线 ${edge.uuid} 的端点不存在，已跳过布局。`,
      )
      return
    }
    const targets = adjacency.get(source.id) ?? new Set<string>()
    if (targets.has(target.id)) return
    targets.add(target.id)
    adjacency.set(source.id, targets)
    const sources = predecessors.get(target.id) ?? new Set<string>()
    sources.add(source.id)
    predecessors.set(target.id, sources)
    indegree.set(target.id, (indegree.get(target.id) ?? 0) + 1)
  })

  const ready = entities.filter((entity) => indegree.get(entity.id) === 0).sort(compareEntities)
  const visited = new Set<string>()
  while (ready.length) {
    const entity = ready.shift()!
    visited.add(entity.id)
    ;[...(adjacency.get(entity.id) ?? [])]
      .map((uuid) => entitiesById.get(uuid)!)
      .sort(compareEntities)
      .forEach((target) => {
        target.rank = Math.max(target.rank, entity.rank + 1)
        const next = (indegree.get(target.id) ?? 0) - 1
        indegree.set(target.id, next)
        if (next === 0) {
          ready.push(target)
          ready.sort(compareEntities)
        }
      })
  }
  if (visited.size !== entities.length) {
    const lastRank = Math.max(0, ...entities.filter((entity) => visited.has(entity.id)).map((entity) => entity.rank))
    entities
      .filter((entity) => !visited.has(entity.id))
      .forEach((entity) => {
        entity.rank = lastRank + 1
      })
    warnings.push('检测到循环依赖；相关节点放到了最后一层。')
  }

  const maxRank = Math.max(0, ...entities.map((entity) => entity.rank))
  const ranks = Array.from({ length: maxRank + 1 }, (_, rank) =>
    entities.filter((entity) => entity.rank === rank).sort(compareEntities),
  )

  const orderByEntity = new Map<string, number>()
  ranks.forEach((rankEntities, rank) => {
    if (rank > 0) {
      const barycenter = (entity: LayoutEntity) => {
        const prior = [...(predecessors.get(entity.id) ?? [])]
          .map((uuid) => orderByEntity.get(uuid))
          .filter((value): value is number => value !== undefined)
        return prior.length ? prior.reduce((sum, value) => sum + value, 0) / prior.length : Number.MAX_SAFE_INTEGER
      }
      rankEntities.sort((left, right) => barycenter(left) - barycenter(right) || compareEntities(left, right))
    }
    rankEntities.forEach((entity, index) => orderByEntity.set(entity.id, index))
  })

  const bandCount = Math.floor(maxRank / ranksPerBand) + 1
  const bandHeights = Array.from({ length: bandCount }, (_, band) => {
    const firstRank = band * ranksPerBand
    const lastRank = Math.min(maxRank, firstRank + ranksPerBand - 1)
    return Math.max(
      NODE_HEIGHT,
      ...ranks
        .slice(firstRank, lastRank + 1)
        .map((rankEntities) => rankEntities.reduce((sum, entity) => sum + entity.height, 0) + Math.max(0, rankEntities.length - 1) * ROW_GAP),
    )
  })
  const bandTops: number[] = []
  // 第一行上方给最外层区域框的标题留空
  let nextBandTop = STAGE_PADDING + (maxDepth ? GROUP_FRAME_TOP + (maxDepth - 1) * NEST_TOP : 0)
  bandHeights.forEach((height, band) => {
    bandTops[band] = nextBandTop
    nextBandTop += height + (band < bandHeights.length - 1 ? BAND_GAP + maxDepth * (NEST_TOP + NEST_BOTTOM) : 0)
  })

  const usedColumns = Math.min(maxRank + 1, ranksPerBand)
  ranks.forEach((rankEntities, rank) => {
    const band = Math.floor(rank / ranksPerBand)
    const slot = rank % ranksPerBand
    const visualSlot = band % 2 === 0 ? slot : ranksPerBand - slot - 1
    let y = bandTops[band]
    rankEntities.forEach((entity) => {
      entity.band = band
      entity.x = STAGE_PADDING + visualSlot * (GROUP_WIDTH + COLUMN_GAP) + (GROUP_WIDTH - entity.width) / 2
      entity.y = y
      y += entity.height + ROW_GAP
    })
  })

  const positionedNodes: PositionedNode[] = entities.map((entity) => {
    const groupUuids: string[] = []
    const seen = new Set<string>()
    let parentUuid = entity.node.parentUuid
    while (parentUuid && groupIds.has(parentUuid) && !seen.has(parentUuid)) {
      seen.add(parentUuid)
      groupUuids.push(parentUuid)
      parentUuid = nodesByUuid.get(parentUuid)?.parentUuid
    }
    return {
      node: entity.node,
      x: entity.x,
      y: entity.y,
      width: entity.width,
      height: entity.height,
      rank: entity.rank,
      band: entity.band,
      groupUuid: groupUuids[0],
      groupUuids,
    }
  })

  const positionedGroups: PositionedGroup[] = groupNodes.map((groupNode) => {
    const children = positionedNodes.filter((node) => node.groupUuids.includes(groupNode.uuid))
    const depth = groupDepth(groupNode.uuid)
    return {
      node: groupNode,
      depth,
      rank: children.length ? Math.min(...children.map((node) => node.rank)) : 0,
      band: children.length ? Math.min(...children.map((node) => node.band)) : 0,
      children,
      frames: children.length ? groupFrames(children, positionedNodes, depth) : [],
    }
  })

  const positionedByUuid = new Map(positionedNodes.map((node) => [node.node.uuid, node]))
  const positionedEdges = edges.flatMap((edge) => {
    const source = positionedByUuid.get(edge.source)
    const target = positionedByUuid.get(edge.target)
    return source && target ? [{ edge, source, target, path: edgePath(source, target) }] : []
  })

  const width = Math.max(
    STAGE_PADDING * 2 + usedColumns * GROUP_WIDTH + Math.max(0, usedColumns - 1) * COLUMN_GAP,
    ...positionedGroups.flatMap((group) => group.frames.map((frame) => frame.x + frame.width + STAGE_PADDING)),
  )
  const height = Math.max(
    nextBandTop + STAGE_PADDING,
    ...positionedGroups.flatMap((group) => group.frames.map((frame) => frame.y + frame.height + STAGE_PADDING)),
  )

  return {
    width,
    height,
    nodes: positionedNodes,
    groups: positionedGroups,
    edges: positionedEdges,
    warnings: [...new Set(warnings)],
    ranksPerBand,
  }
}

export interface NodeOffset {
  x: number
  y: number
}

/** 拖动后的偏移：节点平移、区域框重算、连线重画。 */
export function applyNodeOffsets(layout: DagLayout, offsets: Record<string, NodeOffset>): DagLayout {
  const nodes = layout.nodes.map((positioned) => {
    const offset = offsets[positioned.node.uuid]
    return offset ? { ...positioned, x: positioned.x + offset.x, y: positioned.y + offset.y } : positioned
  })
  const byUuid = new Map(nodes.map((node) => [node.node.uuid, node]))
  const groups = layout.groups.map((group) => {
    const children = group.children.map((child) => byUuid.get(child.node.uuid) ?? child)
    return { ...group, children, frames: children.length ? groupFrames(children, nodes, group.depth) : group.frames }
  })
  const edges = layout.edges.map((positioned) => {
    const source = byUuid.get(positioned.source.node.uuid) ?? positioned.source
    const target = byUuid.get(positioned.target.node.uuid) ?? positioned.target
    return { ...positioned, source, target, path: edgePath(source, target) }
  })
  return { ...layout, nodes, groups, edges }
}
