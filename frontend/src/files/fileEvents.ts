/** Maps cluster events to file tree refreshes: a write refreshes its parent directory, a shell result refreshes the expanded tree. */

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

/** @param row - a cluster event as delivered by the event stream: { type, data: { name | tool_name, tool_input | input } }. */
export function createFileEvents(targets: FileEventTargets, bashMs: number = BASH_REFRESH_MS): FileEvents {
  let cooling: ReturnType<typeof setTimeout> | null = null
  let pending = false

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
      if (event.type !== 'tool_call' || typeof name !== 'string' || !WRITE_TOOLS.has(name)) return
      const filePath = record(data.tool_input ?? data.input)?.file_path
      const root = targets.root()
      if (typeof filePath !== 'string' || root === null) return
      const parent = parentInRoot(filePath, root)
      if (parent !== null && targets.isLoaded(parent)) targets.refresh(parent)
    },
    dispose() {
      if (cooling !== null) clearTimeout(cooling)
      cooling = null
      pending = false
    },
  }
}
