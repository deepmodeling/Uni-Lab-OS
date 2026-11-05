import { describe, expect, it } from 'vitest'
import type { MaterialAggregate } from '../api/types'
import { ancestorsOf, buildMaterialTree, countTree, filterMaterialTree, materialLabel, type MaterialTreeNode } from './materialTree'

function material(uuid: string, extra: { parent?: string; name?: string; display?: string; template?: string; sites?: [string, string | null][] } = {}): MaterialAggregate {
  return {
    material: {
      material_uuid: uuid,
      resource_id: uuid,
      name: extra.name ?? uuid,
      display_name: extra.display ?? '',
      template_name: extra.template ?? 'plate',
      template_uuid: 'tpl',
      parent_material_uuid: extra.parent ?? null,
      resource_type: 'container',
      created_at_ms: 0,
      updated_at_ms: 0,
      version: 1,
    },
    position: {},
    position_version: 1,
    data: { content_version: 1, state_hash: 'h', updated_at_ms: 0, version: 1 },
    sites: (extra.sites ?? []).map(([label, occupant], index) => ({
      site_uuid: `${uuid}-site-${label}`,
      template_name: 'site',
      site_index: index,
      label,
      owner_material_uuid: uuid,
      occupied_material_uuid: occupant,
      ordinal: index,
      changed_at_ms: 0,
      created_at_ms: 0,
      updated_at_ms: 0,
      version: 1,
    })),
    state_hash: 'h',
  } as MaterialAggregate
}

const shape = (nodes: MaterialTreeNode[]): unknown =>
  nodes.map((node) => [node.material.material.material_uuid, shape(node.children)])

const materials = [
  material('deck', { display: 'Deck', sites: [['A1', 'plate-1'], ['A2', null]] }),
  material('plate-1', { parent: 'deck', name: 'Plate 1' }),
  material('tip-rack', { parent: 'deck', name: 'Tips' }),
  material('robot', { name: 'Arm', template: 'arm' }),
  // 占用关系优先于结构父子：plate-2 结构上挂 robot，但放在 deck 的库位里
  material('plate-2', { parent: 'robot', name: 'Plate 2' }),
]
materials[0].sites!.push({ ...materials[0].sites![1], site_uuid: 'deck-site-A3', label: 'A3', ordinal: 2, occupied_material_uuid: 'plate-2' })

describe('buildMaterialTree', () => {
  it('nests by parent and by site occupancy, sorting siblings by label', () => {
    expect(shape(buildMaterialTree(materials))).toEqual([
      ['robot', []],
      ['deck', [['plate-1', []], ['plate-2', []], ['tip-rack', []]]],
    ])
    expect(countTree(buildMaterialTree(materials))).toBe(5)
  })

  it('survives parent cycles', () => {
    const tree = buildMaterialTree([material('a', { parent: 'b' }), material('b', { parent: 'a' })])
    expect(countTree(tree)).toBe(2)
  })
})

describe('filterMaterialTree / labels / ancestors', () => {
  const tree = buildMaterialTree(materials)

  it('keeps matching nodes with their ancestor path and matching sites', () => {
    expect(shape(filterMaterialTree(tree, 'tips'))).toEqual([['deck', [['tip-rack', []]]]])
    const bySite = filterMaterialTree(tree, 'a2')
    expect(bySite[0].sites.map((site) => site.label)).toEqual(['A2'])
    expect(filterMaterialTree(tree, '  ')).toBe(tree)
    expect(filterMaterialTree(tree, 'nothing-here')).toEqual([])
  })

  it('labels prefer display_name and mention the template', () => {
    expect(materialLabel(materials[0])).toMatchObject({ primary: 'Deck', secondary: 'plate · container' })
    expect(materialLabel(materials[1]).primary).toBe('Plate 1')
  })

  it('walks ancestors through site occupancy first', () => {
    expect(ancestorsOf(materials, 'plate-2')).toEqual(['deck'])
    expect(ancestorsOf(materials, 'tip-rack')).toEqual(['deck'])
    expect(ancestorsOf(materials, 'robot')).toEqual([])
  })
})
