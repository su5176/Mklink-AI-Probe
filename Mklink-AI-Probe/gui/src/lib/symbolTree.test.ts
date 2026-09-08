import { describe, expect, it } from 'vitest'
import type { SymbolContainerDescriptor, SymbolDescriptor } from '../types/mklink'
import { buildSymbolTree, collectBranchKeys, visibleSymbolRows } from './symbolTree'

function symbol(path: string, parentPath: string | null = null): SymbolDescriptor {
  return {
    path,
    address: 0x20000000,
    type_name: 'float',
    scalar_kind: 'float',
    size: 4,
    writable: true,
    enum_values: {},
    parent_path: parentPath,
  }
}

describe('symbolTree', () => {
  it('shows the union of comma-separated search terms', () => {
    const rows = visibleSymbolRows(buildSymbolTree([
      symbol('speed'), symbol('motor.current', 'motor'), symbol('temperature'), symbol('other'),
    ]), { expanded: new Set(), selected: new Set(), query: 'speed，current;temperature', selectedOnly: false })
    expect(rows.filter(row => row.node.kind === 'leaf').map(row => row.node.key)).toEqual([
      'speed', 'motor.current', 'temperature',
    ])
  })
  it('builds nested structure and array branches while keeping scalars as leaves', () => {
    const roots = buildSymbolTree([
      symbol('gain'),
      symbol('controller.enabled', 'controller'),
      symbol('controller.channels[0].value', 'controller'),
      symbol('controller.channels[1].value', 'controller'),
    ])

    expect(roots.map(node => [node.key, node.kind])).toEqual([
      ['gain', 'leaf'],
      ['controller', 'branch'],
    ])
    const rows = visibleSymbolRows(roots, {
      expanded: new Set(['controller', 'controller.channels', 'controller.channels[0]']),
      selected: new Set<string>(),
      query: '',
      selectedOnly: false,
    })
    expect(rows.map(row => [row.node.key, row.depth])).toContainEqual([
      'controller.channels[0].value', 3,
    ])
    expect(collectBranchKeys(roots)).toEqual(new Set([
      'controller',
      'controller.channels',
      'controller.channels[0]',
      'controller.channels[1]',
    ]))
  })

  it('keeps structured roots collapsed and does not expose their leaves by default', () => {
    const rows = visibleSymbolRows(buildSymbolTree([
      symbol('gain'),
      symbol('controller.target', 'controller'),
    ]), {
      expanded: new Set<string>(),
      selected: new Set<string>(),
      query: '',
      selectedOnly: false,
    })

    expect(rows.map(row => row.node.key)).toEqual(['gain', 'controller'])
  })

  it('auto-expands search matches and composes search with selected-only', () => {
    const roots = buildSymbolTree([
      symbol('controller.channels[0].value', 'controller'),
      symbol('controller.channels[1].status', 'controller'),
      symbol('gain'),
    ])
    const rows = visibleSymbolRows(roots, {
      expanded: new Set<string>(),
      selected: new Set(['controller.channels[0].value']),
      query: 'value',
      selectedOnly: true,
    })

    expect(rows.map(row => row.node.key)).toEqual([
      'controller',
      'controller.channels',
      'controller.channels[0]',
      'controller.channels[0].value',
    ])
    expect(rows.filter(row => row.node.kind === 'branch').every(row => row.expanded)).toBe(true)
  })

  it('matches type names during search', () => {
    const integer = { ...symbol('counter'), type_name: 'uint32_t' }
    const rows = visibleSymbolRows(buildSymbolTree([integer, symbol('gain')]), {
      expanded: new Set<string>(),
      selected: new Set<string>(),
      query: 'uint32',
      selectedOnly: false,
    })

    expect(rows.map(row => row.node.key)).toEqual(['counter'])
  })

  it('shows unresolved aggregate containers as searchable terminal rows', () => {
    const container: SymbolContainerDescriptor = {
      path: 'data_save',
      address: 0x20000648,
      type_name: 'DATASAVE_TYPEDEF',
      size: 32,
      reason: 'unsupported_layout',
    }
    const roots = buildSymbolTree([], [container])
    const rows = visibleSymbolRows(roots, {
      expanded: new Set<string>(),
      selected: new Set<string>(),
      query: 'datasave',
      selectedOnly: false,
    })

    expect(rows).toHaveLength(1)
    expect(rows[0].node.kind).toBe('container')
    expect(rows[0].node.container).toEqual(container)
    expect(rows[0].node.leafCount).toBe(0)
  })

  it('keeps a 4660-leaf catalog bounded while roots are collapsed', () => {
    const items = Array.from({ length: 4660 }, (_, index) =>
      symbol(`root${Math.floor(index / 256)}.values[${index % 256}]`, `root${Math.floor(index / 256)}`),
    )
    const rows = visibleSymbolRows(buildSymbolTree(items), {
      expanded: new Set<string>(),
      selected: new Set<string>(),
      query: '',
      selectedOnly: false,
    })

    expect(rows.length).toBeLessThan(32)
    expect(rows.every(row => row.node.kind === 'branch')).toBe(true)
  })
})
