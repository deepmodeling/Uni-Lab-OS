/**
 * 物料聚合列表 → 层级树（移植自 v1.0 MaterialHierarchyTree 的 buildTree / filterTree）。
 *
 * 结构父子用 `material.parent_material_uuid`；库位占用（`sites[].occupied_material_uuid`）
 * 让占用物料挂到库位所属物料下面，并按库位顺序显示，空库位单独成行。
 */
import type { MaterialAggregate, MaterialSite } from '../api/types'

export interface MaterialTreeNode {
  material: MaterialAggregate
  children: MaterialTreeNode[]
  /** 本物料拥有的库位（按 ordinal）。 */
  sites: MaterialSite[]
}

export interface MaterialLabel {
  primary: string
  secondary: string
  text: string
}

export function materialUuid(material: MaterialAggregate): string {
  return material.material.material_uuid
}

export function materialLabel(material: MaterialAggregate): MaterialLabel {
  const identity = material.material
  const primary = identity.display_name || identity.name || identity.resource_id || identity.material_uuid
  const secondary = [identity.template_name, identity.resource_type].filter(Boolean).join(' · ')
  return { primary, secondary, text: [primary, identity.resource_id, secondary].filter(Boolean).join(' ') }
}

function sortedSites(material: MaterialAggregate): MaterialSite[] {
  return [...(material.sites ?? [])].sort(
    (left, right) => (left.ordinal ?? 0) - (right.ordinal ?? 0) || String(left.label).localeCompare(String(right.label)),
  )
}

export function buildMaterialTree(materials: MaterialAggregate[]): MaterialTreeNode[] {
  const known = new Set(materials.map(materialUuid))
  const siteOwnerByOccupant = new Map<string, string>()
  for (const owner of materials) {
    for (const site of owner.sites ?? []) {
      if (site.occupied_material_uuid) siteOwnerByOccupant.set(site.occupied_material_uuid, materialUuid(owner))
    }
  }
  const byParent = new Map<string, MaterialAggregate[]>()
  for (const material of materials) {
    const uuid = materialUuid(material)
    const siteOwner = siteOwnerByOccupant.get(uuid)
    const parent = material.material.parent_material_uuid
    const key = siteOwner && known.has(siteOwner) && siteOwner !== uuid
      ? siteOwner
      : parent && known.has(parent) && parent !== uuid
        ? parent
        : '__root__'
    byParent.set(key, [...(byParent.get(key) ?? []), material])
  }
  const byLabel = (left: MaterialAggregate, right: MaterialAggregate) =>
    materialLabel(left).primary.localeCompare(materialLabel(right).primary, 'zh-CN', { numeric: true })
  const seen = new Set<string>()
  const build = (material: MaterialAggregate): MaterialTreeNode => {
    const uuid = materialUuid(material)
    seen.add(uuid)
    return {
      material,
      // 防御环：同一物料只出现一次
      children: (byParent.get(uuid) ?? []).filter((child) => !seen.has(materialUuid(child))).sort(byLabel).map(build),
      sites: sortedSites(material),
    }
  }
  const roots = (byParent.get('__root__') ?? []).sort(byLabel).map(build)
  // 指向彼此的孤立环（没有任何一个落到根）也要显示出来：逐个补成根，已出现过的跳过
  for (const material of [...materials].sort(byLabel)) {
    if (!seen.has(materialUuid(material))) roots.push(build(material))
  }
  return roots
}

/** 关键字过滤：保留命中节点、它们的祖先路径与命中的库位；不修改原树。 */
export function filterMaterialTree(nodes: MaterialTreeNode[], query: string): MaterialTreeNode[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return nodes
  return nodes.flatMap((node) => {
    const children = filterMaterialTree(node.children, query)
    const sites = node.sites.filter((site) =>
      [site.site_uuid, site.label, String(site.site_index)].join(' ').toLowerCase().includes(needle),
    )
    const identity = node.material.material
    const own = [materialLabel(node.material).text, identity.material_uuid, identity.barcode, identity.class_name]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(needle)
    if (own) return [node]
    return sites.length || children.length ? [{ ...node, children, sites }] : []
  })
}

export function countTree(nodes: MaterialTreeNode[]): number {
  return nodes.reduce((sum, node) => sum + 1 + countTree(node.children), 0)
}

/** 选中节点的祖先 uuid（展开路径用）。 */
export function ancestorsOf(materials: MaterialAggregate[], uuid: string): string[] {
  const byUuid = new Map(materials.map((material) => [materialUuid(material), material]))
  const siteOwnerByOccupant = new Map<string, string>()
  for (const owner of materials) {
    for (const site of owner.sites ?? []) {
      if (site.occupied_material_uuid) siteOwnerByOccupant.set(site.occupied_material_uuid, materialUuid(owner))
    }
  }
  const result: string[] = []
  const seen = new Set<string>([uuid])
  let current = uuid
  for (;;) {
    const parent = siteOwnerByOccupant.get(current) ?? byUuid.get(current)?.material.parent_material_uuid ?? undefined
    if (!parent || seen.has(parent) || !byUuid.has(parent)) break
    result.push(parent)
    seen.add(parent)
    current = parent
  }
  return result
}
