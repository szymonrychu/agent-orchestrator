/**
 * Pure layout state of one cluster workspace: three panel sizes (percent) and which panels are minimized.
 * Every function returns a new layout, or the same object when the request changes nothing.
 */
export const PANELS = ['files', 'chat', 'terminal'] as const
export type PanelName = (typeof PANELS)[number]
export type Sizes = [number, number, number]

export const DEFAULT_SIZES: Sizes = [25, 45, 30]

/** The part of a layout that maximize remembers. */
export interface Snapshot {
  sizes: Sizes
  collapsed: ReadonlySet<PanelName>
}

export interface PanelLayout extends Snapshot {
  /** The layout from before the last maximize; set exactly while a maximize can still be undone. */
  before: Snapshot | null
}

export function startLayout(): PanelLayout {
  return { sizes: [...DEFAULT_SIZES], collapsed: new Set(), before: null }
}

const snapshot = (l: Snapshot): Snapshot => ({ sizes: l.sizes, collapsed: l.collapsed })

/** Minimize `p`; refused (same layout back) when `p` is already minimized or is the last expanded panel. */
export function minimize(l: PanelLayout, p: PanelName): PanelLayout {
  if (l.collapsed.has(p) || l.collapsed.size >= PANELS.length - 1) return l
  return { sizes: l.sizes, collapsed: new Set([...l.collapsed, p]), before: null }
}

/** Expand `p` and collapse the other two, remembering the layout to return to. */
export function maximize(l: PanelLayout, p: PanelName): PanelLayout {
  const others = PANELS.filter((q) => q !== p)
  if (l.collapsed.size === others.length && !l.collapsed.has(p)) return l
  return { sizes: l.sizes, collapsed: new Set(others), before: l.before ?? snapshot(l) }
}

/** Expand a minimized panel; it returns to the size it had before it was minimized. */
export function restore(l: PanelLayout, p: PanelName): PanelLayout {
  if (!l.collapsed.has(p)) return l
  return { sizes: l.sizes, collapsed: new Set([...l.collapsed].filter((q) => q !== p)), before: null }
}

/** Undo a maximize: back to the layout from before it. */
export function restoreMaximized(l: PanelLayout): PanelLayout {
  return l.before ? { ...l.before, before: null } : l
}

/** Default sizes, nothing minimized. */
export function reset(): PanelLayout {
  return startLayout()
}

/**
 * Adopt a layout the user produced by dragging: `sizes[i]` replaces the size of panel i when it is a number,
 * and `collapsed` becomes the minimized set unless that would minimize every panel.
 */
export function resize(l: PanelLayout, sizes: ReadonlyArray<number | null>, collapsed: Iterable<PanelName> = l.collapsed): PanelLayout {
  const next = new Set(collapsed)
  return {
    sizes: sizes.map((s, i) => (s !== null && Number.isFinite(s) && s > 0 ? s : l.sizes[i])) as Sizes,
    collapsed: next.size >= PANELS.length ? l.collapsed : next,
    before: null,
  }
}

/** The sizes scaled to add up to 100, as the panel library expects. */
export function normalisedSizes(sizes: Sizes): Sizes {
  const total = sizes[0] + sizes[1] + sizes[2]
  return sizes.map((s) => (s * 100) / total) as Sizes
}

type StoredSnapshot = { sizes: Sizes; collapsed: PanelName[] }
export type StoredLayout = StoredSnapshot & { before: StoredSnapshot | null }

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

function parseSnapshot(v: unknown): Snapshot | null {
  if (!isRecord(v) || !Array.isArray(v.sizes) || !Array.isArray(v.collapsed)) return null
  if (v.sizes.length !== PANELS.length || !v.sizes.every((s) => typeof s === 'number' && Number.isFinite(s) && s > 0)) return null
  const names = v.collapsed
  if (!names.every((n): n is PanelName => PANELS.includes(n as PanelName)) || new Set(names).size !== names.length || names.length >= PANELS.length) return null
  return { sizes: v.sizes as Sizes, collapsed: new Set(names) }
}

/** The storable form of a layout. */
export function serialiseLayout(l: PanelLayout): StoredLayout {
  const plain = (s: Snapshot): StoredSnapshot => ({ sizes: [...s.sizes], collapsed: PANELS.filter((p) => s.collapsed.has(p)) })
  return { ...plain(l), before: l.before ? plain(l.before) : null }
}

/** Validate stored data; anything malformed gives the default layout. */
export function parseLayout(v: unknown): PanelLayout {
  const now = parseSnapshot(v)
  if (!now) return startLayout()
  const raw = (v as Record<string, unknown>).before
  const before = raw === undefined || raw === null ? null : parseSnapshot(raw)
  if (raw !== undefined && raw !== null && !before) return startLayout()
  return { ...now, before }
}
