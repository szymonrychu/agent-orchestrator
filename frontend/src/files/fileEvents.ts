/** Maps cluster events to file tree refreshes: a finished write refreshes its parent directory, a shell result refreshes the expanded tree. */

const WRITE_TOOLS = new Set(['Write', 'Edit', 'NotebookEdit'])
const BASH_TOOL = 'Bash'
export const BASH_REFRESH_MS = 1000

export interface FileEventTargets {
  /** Absolute real path of the workdir from the latest listing, or null before one arrived. */
  root: () => string | null
  /** Whether the directory (relative path) is in the tree cache. */
  isLoaded: (path: string) => boolean
  refresh: (path: string) => void
  refreshExpanded: () => void
  /** A write tool finished on this root-relative file path. */
  written?: (path: string) => void
}

export interface FileEvents {
  handle: (row: unknown) => void
  dispose: () => void
}

const record = (v: unknown): Record<string, unknown> | null => (v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : null)

/** Relative parent directory of an absolute file path inside `root`, or null when it is outside. */
export function parentInRoot(filePath: string, root: string): string | null {
  const base = root.replace(/\/+$/, '')
  if (!filePath.startsWith(`${base}/`)) return null
  const parts = filePath.slice(base.length + 1).split('/').filter((p) => p !== '')
  if (parts.length === 0 || parts.includes('..') || parts.includes('.')) return null
  return parts.slice(0, -1).join('/')
}

/** The directory itself when loaded, else the closest loaded ancestor, ending at the root. */
function nearestLoaded(dir: string, isLoaded: (path: string) => boolean): string {
  let p = dir
  while (p !== '' && !isLoaded(p)) p = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : ''
  return p
}

/** Root-relative path of an absolute file path already known to be inside `root`. */
function relativeInRoot(filePath: string, root: string): string {
  return filePath.slice(root.replace(/\/+$/, '').length + 1)
}

/** @param row - a cluster event as delivered by the event stream: { type, data: { name | tool_name, tool_input | input } }. */
export function createFileEvents(targets: FileEventTargets, bashMs: number = BASH_REFRESH_MS): FileEvents {
  let cooling: ReturnType<typeof setTimeout> | null = null
  let pending = false
  // Write calls by tool use id, awaiting their result: the call fires before the tool runs.
  const writes = new Map<string, string>()

  const runBash = () => {
    targets.refreshExpanded()
    cooling = setTimeout(() => {
      cooling = null
      if (pending) {
        pending = false
        runBash()
      }
    }, bashMs)
  }

  return {
    handle(row) {
      const event = record(row)
      const data = record(event?.data)
      if (!event || !data) return
      const name = data.name ?? data.tool_name
      if (event.type === 'tool_result' && name === BASH_TOOL) {
        if (cooling === null) runBash()
        else pending = true
        return
      }
      if (event.type === 'tool_result' && typeof data.tool_use_id === 'string') {
        const path = writes.get(data.tool_use_id)
        if (path === undefined) return
        writes.delete(data.tool_use_id)
        const root = targets.root()
        if (root === null) return
        const parent = parentInRoot(path, root)
        if (parent === null) return
        targets.refresh(nearestLoaded(parent, targets.isLoaded))
        targets.written?.(relativeInRoot(path, root))
        return
      }
      if (event.type !== 'tool_call' || typeof name !== 'string' || !WRITE_TOOLS.has(name)) return
      const input = record(data.tool_input ?? data.input)
      const filePath = input?.file_path ?? input?.notebook_path
      if (typeof filePath === 'string' && typeof data.tool_use_id === 'string') writes.set(data.tool_use_id, filePath)
    },
    dispose() {
      if (cooling !== null) clearTimeout(cooling)
      cooling = null
      pending = false
      writes.clear()
    },
  }
}
