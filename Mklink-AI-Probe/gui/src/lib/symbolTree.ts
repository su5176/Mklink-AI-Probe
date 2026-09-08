import type { SymbolBrowseNode, SymbolContainerDescriptor, SymbolDescriptor } from '../types/mklink'

export interface SymbolTreeNode {
  key: string
  label: string
  kind: 'branch' | 'leaf' | 'container' | 'range'
  descriptor: SymbolDescriptor | null
  container: SymbolContainerDescriptor | null
  browse: SymbolBrowseNode | null
  children: SymbolTreeNode[]
  leafCount: number
  childCount: number | null
}

export interface VisibleSymbolRow {
  node: SymbolTreeNode
  depth: number
  expanded: boolean
  selectedLeafCount: number
}

export interface VisibleSymbolOptions {
  expanded: ReadonlySet<string>
  collapsed?: ReadonlySet<string>
  selected: ReadonlySet<string>
  query: string
  selectedOnly: boolean
}

interface MutableSymbolTreeNode extends SymbolTreeNode {
  childIndex: Map<string, MutableSymbolTreeNode>
  children: MutableSymbolTreeNode[]
}

function pathTokens(path: string): string[] {
  return path.match(/[^.[\]]+|\[\d+\]/g) ?? [path]
}

function appendPath(parent: string, token: string): string {
  if (!parent) return token
  return token.startsWith('[') ? `${parent}${token}` : `${parent}.${token}`
}

function createNode(
  key: string,
  label: string,
  descriptor: SymbolDescriptor | null,
  container: SymbolContainerDescriptor | null = null,
): MutableSymbolTreeNode {
  return {
    key,
    label,
    kind: descriptor ? 'leaf' : container ? 'container' : 'branch',
    descriptor,
    container,
    browse: null,
    children: [],
    childIndex: new Map(),
    leafCount: descriptor ? 1 : 0,
    childCount: null,
  }
}

function finalizeNode(node: MutableSymbolTreeNode): SymbolTreeNode {
  const children = node.children.map(finalizeNode)
  return {
    key: node.key,
    label: node.label,
    kind: node.kind,
    descriptor: node.descriptor,
    container: node.container,
    browse: node.browse,
    children,
    leafCount: node.kind === 'leaf'
      ? 1
        : children.reduce((total, child) => total + child.leafCount, 0),
    childCount: node.childCount,
  }
}

export function buildBrowseTree(
  roots: readonly SymbolBrowseNode[],
  childPages: ReadonlyMap<string, readonly SymbolBrowseNode[]>,
): SymbolTreeNode[] {
  function convert(entry: SymbolBrowseNode): SymbolTreeNode {
    const children = (childPages.get(entry.key) ?? []).map(convert)
    return {
      key: entry.key,
      label: entry.label,
      kind: entry.kind,
      descriptor: entry.descriptor,
      container: entry.container,
      browse: entry,
      children,
      leafCount: entry.kind === 'leaf'
        ? 1
        : children.reduce((total, child) => total + child.leafCount, 0),
      childCount: entry.child_count,
    }
  }
  return roots.map(convert)
}

export function buildSymbolTree(
  items: readonly SymbolDescriptor[],
  containers: readonly SymbolContainerDescriptor[] = [],
): SymbolTreeNode[] {
  const roots: MutableSymbolTreeNode[] = []
  const rootIndex = new Map<string, MutableSymbolTreeNode>()

  for (const descriptor of items) {
    const tokens = pathTokens(descriptor.path)
    let parentKey = ''
    let siblings = roots
    let siblingIndex = rootIndex

    tokens.forEach((token, index) => {
      const key = appendPath(parentKey, token)
      const isLeaf = index === tokens.length - 1
      let node = siblingIndex.get(key)
      if (!node) {
        node = createNode(key, token, isLeaf ? descriptor : null)
        siblingIndex.set(key, node)
        siblings.push(node)
      }
      parentKey = key
      siblings = node.children
      siblingIndex = node.childIndex
    })
  }

  for (const container of containers) {
    const tokens = pathTokens(container.path)
    let parentKey = ''
    let siblings = roots
    let siblingIndex = rootIndex
    tokens.forEach((token, index) => {
      const key = appendPath(parentKey, token)
      const isContainer = index === tokens.length - 1
      let node = siblingIndex.get(key)
      if (!node) {
        node = createNode(key, token, null, isContainer ? container : null)
        siblingIndex.set(key, node)
        siblings.push(node)
      } else if (isContainer && node.kind === 'branch' && node.children.length === 0) {
        node.kind = 'container'
        node.container = container
      }
      parentKey = key
      siblings = node.children
      siblingIndex = node.childIndex
    })
  }

  return roots.map(finalizeNode)
}

export function visibleSymbolRows(
  roots: readonly SymbolTreeNode[],
  options: VisibleSymbolOptions,
): VisibleSymbolRow[] {
  const query = options.query.trim().toLocaleLowerCase()
  const terms = query.split(/[,，;；\n]+/).map(term => term.trim()).filter(Boolean)
  const matches = (path: string, type: string) => !terms.length || terms.some(term => (
    path.toLocaleLowerCase().includes(term) || type.toLocaleLowerCase().includes(term)
  ))
  const forceExpanded = Boolean(query) || options.selectedOnly
  const visible = new Map<string, boolean>()
  const selectedCounts = new Map<string, number>()

  function isVisible(node: SymbolTreeNode): boolean {
    const cached = visible.get(node.key)
    if (cached !== undefined) return cached
    let result: boolean
    if (node.kind === 'leaf') {
      const descriptor = node.descriptor
      const selectedMatch = !options.selectedOnly || options.selected.has(node.key)
      const queryMatch = !query || Boolean(
        descriptor
        && matches(descriptor.path, descriptor.type_name),
      )
      result = selectedMatch && queryMatch
    } else if (node.kind === 'container') {
      const container = node.container
      result = !options.selectedOnly && Boolean(
        container
        && matches(container.path, container.type_name),
      )
    } else {
      result = query ? node.children.some(isVisible) : true
    }
    visible.set(node.key, result)
    return result
  }

  function selectedLeafCount(node: SymbolTreeNode): number {
    const cached = selectedCounts.get(node.key)
    if (cached !== undefined) return cached
    const count = node.kind === 'leaf'
      ? Number(options.selected.has(node.key))
      : node.kind === 'branch' || node.kind === 'range'
        ? node.children.reduce((total, child) => total + selectedLeafCount(child), 0)
        : 0
    selectedCounts.set(node.key, count)
    return count
  }

  const rows: VisibleSymbolRow[] = []
  function appendVisible(node: SymbolTreeNode, depth: number): void {
    if (!isVisible(node)) return
    const expandable = node.kind === 'branch' || node.kind === 'range'
    const expanded = expandable && !options.collapsed?.has(node.key)
      && (forceExpanded || options.expanded.has(node.key))
    rows.push({
      node,
      depth,
      expanded,
      selectedLeafCount: selectedLeafCount(node),
    })
    if (!expanded) return
    node.children.forEach(child => appendVisible(child, depth + 1))
  }

  roots.forEach(root => appendVisible(root, 0))
  return rows
}

export function collectBranchKeys(roots: readonly SymbolTreeNode[]): Set<string> {
  const keys = new Set<string>()
  function visit(node: SymbolTreeNode): void {
    if (node.kind !== 'branch' && node.kind !== 'range') return
    keys.add(node.key)
    node.children.forEach(visit)
  }
  roots.forEach(visit)
  return keys
}
