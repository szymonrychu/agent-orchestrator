/** Lazily loaded file tree of one cluster: a cache of directory listings keyed by relative path ("" is the workdir root). */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { listFiles } from '../api/files'
import type { FileEntry } from '../api/files'

export interface DirState {
  entries: FileEntry[]
  /** Offset of the next page, null on the last page. */
  nextOffset: number | null
  loading: boolean
  error: string | null
}

export interface TreeState {
  /** Absolute real path of the workdir, from the latest listing. */
  root: string | null
  dirs: Readonly<Record<string, DirState>>
  /** Relative paths of the expanded directories (the root is always shown and not listed here). */
  expanded: readonly string[]
  /** The workdir does not exist yet. */
  missing: boolean
}

export interface FileTree {
  state: TreeState
  expand: (path: string) => void
  collapse: (path: string) => void
  /** Apply the full expanded set reported by the tree view. */
  setExpanded: (paths: readonly string[]) => void
  loadMore: (path: string) => void
  refresh: (path: string) => void
  /** Re-fetch the root and every expanded directory. */
  refreshExpanded: () => void
  /** Quiet refresh of the root and every expanded directory: skips directories mid-load and retries a failed or missing root. */
  pollExpanded: () => void
  isLoaded: (path: string) => boolean
  root: () => string | null
}

const initial = (): TreeState => ({ root: null, dirs: {}, expanded: [], missing: false })
const EMPTY_DIR: DirState = { entries: [], nextOffset: null, loading: false, error: null }

export const joinPath = (dir: string, name: string): string => (dir === '' ? name : `${dir}/${name}`)

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return `${e.code}: ${e.message}`
  return e instanceof Error ? e.message : String(e)
}

/** Append a page, dropping names already listed (the directory may have changed between pages). */
function append(entries: FileEntry[], page: FileEntry[]): FileEntry[] {
  const seen = new Set(entries.map((e) => e.name))
  return [...entries, ...page.filter((e) => !seen.has(e.name))]
}

const sameEntries = (a: FileEntry[], b: FileEntry[]): boolean =>
  a.length === b.length && a.every((e, i) => e.name === b[i].name && e.type === b[i].type && e.size === b[i].size && e.mtime === b[i].mtime)

export function useFileTree(clusterId: string): FileTree {
  const [state, setState] = useState<TreeState>(initial)
  const ref = useRef(state)
  const epoch = useRef(0)
  const seq = useRef<Record<string, number>>({})
  // Token of the request in flight per path; a refresh does not mark the dir as loading, so it is tracked here too.
  const busy = useRef<Record<string, number>>({})

  const update = useCallback((fn: (s: TreeState) => TreeState) => {
    ref.current = fn(ref.current)
    setState(ref.current)
  }, [])
  const patchDir = useCallback(
    (path: string, fn: (d: DirState) => DirState, extra?: Partial<TreeState>) =>
      update((s) => ({ ...s, ...extra, dirs: { ...s.dirs, [path]: fn(s.dirs[path] ?? EMPTY_DIR) } })),
    [update],
  )

  // The cluster id changing is the epoch boundary: everything from the previous cluster is dropped.
  const [shown, setShown] = useState(clusterId)
  if (shown !== clusterId) {
    setShown(clusterId)
    setState(initial())
  }

  /** first: fetch page one; more: append the next page; refresh: re-fetch page one, then as many pages as were loaded. */
  const load = useCallback(
    (path: string, mode: 'first' | 'more' | 'refresh') => {
      const mine = epoch.current
      const dir = ref.current.dirs[path]
      if (mode === 'more' && (!dir || dir.nextOffset === null || dir.loading)) return
      const token = (seq.current[path] = (seq.current[path] ?? 0) + 1)
      const live = () => epoch.current === mine && seq.current[path] === token
      const wanted = mode === 'refresh' ? (dir?.entries.length ?? 0) : 0
      busy.current[path] = token
      const finish = () => {
        if (busy.current[path] === token) delete busy.current[path]
      }
      // A refresh stays silent until its listing differs, so an idle one causes no state change.
      if (mode !== 'refresh' || !dir) patchDir(path, (d) => ({ ...d, loading: true, error: null }))
      const run = async () => {
        let entries = mode === 'more' ? (dir?.entries ?? []) : []
        let offset: number | undefined = mode === 'more' ? (dir?.nextOffset ?? undefined) : undefined
        for (;;) {
          const page = offset === undefined ? await listFiles(clusterId, path) : await listFiles(clusterId, path, offset)
          if (!live()) return
          entries = offset === undefined ? page.entries : append(entries, page.entries)
          offset = page.next_offset ?? undefined
          const settle = (done: boolean) => {
            const cur = ref.current.dirs[path]
            if (
              done && mode === 'refresh' && cur && !cur.loading && cur.error === null && cur.nextOffset === page.next_offset &&
              ref.current.root === page.root && !(path === '' && ref.current.missing) && sameEntries(cur.entries, entries)
            )
              return
            patchDir(path, () => ({ entries, nextOffset: page.next_offset, loading: !done, error: null }), { root: page.root, ...(path === '' ? { missing: false } : {}) })
          }
          if (offset === undefined || mode !== 'refresh' || entries.length >= wanted) {
            settle(true)
            return finish()
          }
          settle(false)
        }
      }
      run().catch((e: unknown) => {
        finish()
        if (!live()) return
        const missing = path === '' && e instanceof ApiError && e.code === 'not_found'
        patchDir(path, (d) => ({ ...d, loading: false, error: missing ? null : errorText(e) }), path === '' ? { missing } : undefined)
      })
    },
    [clusterId, patchDir],
  )

  useEffect(() => {
    epoch.current += 1
    seq.current = {}
    busy.current = {}
    ref.current = initial()
    load('', 'first')
    return () => {
      epoch.current += 1
    }
  }, [clusterId, load])

  const expand = useCallback(
    (path: string) => {
      if (path === '' || ref.current.expanded.includes(path)) return
      update((s) => ({ ...s, expanded: [...s.expanded, path] }))
      // First expand fetches; a collapsed directory that is cached shows its listing while it is refreshed.
      load(path, ref.current.dirs[path] ? 'refresh' : 'first')
    },
    [load, update],
  )
  const collapse = useCallback((path: string) => update((s) => ({ ...s, expanded: s.expanded.filter((p) => p !== path) })), [update])
  const setExpanded = useCallback(
    (paths: readonly string[]) => {
      const next = new Set(paths)
      for (const p of ref.current.expanded) if (!next.has(p)) collapse(p)
      for (const p of paths) expand(p)
    },
    [collapse, expand],
  )
  const loadMore = useCallback((path: string) => load(path, 'more'), [load])
  const refresh = useCallback((path: string) => load(path, ref.current.dirs[path] ? 'refresh' : 'first'), [load])
  const refreshExpanded = useCallback(() => {
    for (const p of ['', ...ref.current.expanded]) load(p, ref.current.dirs[p] ? 'refresh' : 'first')
  }, [load])
  const pollExpanded = useCallback(() => {
    const { dirs, expanded, missing } = ref.current
    for (const p of ['', ...expanded]) {
      if (p in busy.current || dirs[p]?.loading) continue
      const retry = p === '' && (missing || !dirs[p] || dirs[p].error !== null)
      load(p, dirs[p] && !retry ? 'refresh' : 'first')
    }
  }, [load])
  const isLoaded = useCallback((path: string) => path in ref.current.dirs, [])
  const root = useCallback(() => ref.current.root, [])

  return useMemo(
    () => ({ state, expand, collapse, setExpanded, loadMore, refresh, refreshExpanded, pollExpanded, isLoaded, root }),
    [state, expand, collapse, setExpanded, loadMore, refresh, refreshExpanded, pollExpanded, isLoaded, root],
  )
}
