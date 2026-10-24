/** Filesystem panel: the cluster's shared workdir as a lazily loaded tree with a read-only file preview. */
import Folder from '@mui/icons-material/Folder'
import FolderOpen from '@mui/icons-material/FolderOpen'
import InsertDriveFileOutlined from '@mui/icons-material/InsertDriveFileOutlined'
import Link from '@mui/icons-material/Link'
import Refresh from '@mui/icons-material/Refresh'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import { SimpleTreeView } from '@mui/x-tree-view/SimpleTreeView'
import { TreeItem } from '@mui/x-tree-view/TreeItem'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { eventStreamUrl } from '../api/client'
import { readFile } from '../api/files'
import type { FileContent, FileEntry } from '../api/files'
import { openStream } from '../api/stream'
import { MONO_FONT, TOKENS } from '../theme'
import { createFileEvents } from './fileEvents'
import { errorText, joinPath, useFileTree } from './useFileTree'

const WATCHED_EVENTS = ['tool_call', 'tool_result']

const entryId = (path: string) => `e:${path}`
const moreId = (dir: string) => `more:${dir}`
const loadingId = (dir: string) => `loading:${dir}`
const emptyId = (dir: string) => `empty:${dir}`
const errorId = (dir: string) => `error:${dir}`

type PreviewState =
  | { state: 'loading' }
  | { state: 'loaded'; file: FileContent }
  | { state: 'error'; message: string }
  | { state: 'other' }
type Preview = PreviewState & { path: string; clusterId: string }

const noteSx = { fontSize: `${TOKENS.fontSizes.sm / 16}rem`, color: 'text.secondary' } as const
const preSx = { m: 0, fontFamily: MONO_FONT, fontSize: `${TOKENS.fontSizes.sm / 16}rem`, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', color: 'text.primary' } as const

function Row({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }}>
      {icon}
      <Box component="span" sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {children}
      </Box>
    </Box>
  )
}

function PreviewBody({ preview }: { preview: PreviewState }) {
  switch (preview.state) {
    case 'loading':
      return <Typography sx={noteSx}>loading</Typography>
    case 'error':
      return (
        <Box role="alert" data-testid="file-preview-error" sx={{ ...noteSx, color: 'error.main' }}>
          {preview.message}
        </Box>
      )
    case 'other':
      return (
        <Typography data-testid="file-preview-note" sx={noteSx}>
          Type: other. No preview.
        </Typography>
      )
    case 'loaded': {
      const { file } = preview
      if (file.kind === 'text') {
        return (
          <Box component="pre" data-testid="file-preview-text" sx={preSx}>
            {file.content ?? ''}
          </Box>
        )
      }
      const note = file.kind === 'binary' ? `Binary file, ${file.size} bytes, ${file.mime ?? 'unknown type'}` : `${file.size} bytes, too large to preview`
      return (
        <Typography data-testid="file-preview-note" sx={noteSx}>
          {note}
        </Typography>
      )
    }
  }
}

export function FilesPanel({ clusterId }: { clusterId: string }) {
  const tree = useFileTree(clusterId)
  const { state, setExpanded, loadMore, refresh, refreshExpanded, isLoaded, root } = tree
  const [stored, setPreview] = useState<Preview | null>(null)
  // A preview belongs to the cluster it was opened in: a cluster switch drops it.
  const preview = stored?.clusterId === clusterId ? stored : null
  const previewToken = useRef(0)
  const previewRef = useRef(preview)
  useEffect(() => {
    previewRef.current = preview
  })

  const openFile = useCallback(
    (path: string) => {
      const token = ++previewToken.current
      setPreview((p) => (p?.clusterId === clusterId && p.path === path && p.state === 'loaded' ? p : { path, clusterId, state: 'loading' }))
      readFile(clusterId, path).then(
        (file) => {
          if (previewToken.current === token) setPreview({ path, clusterId, state: 'loaded', file })
        },
        (e: unknown) => {
          if (previewToken.current === token) setPreview({ path, clusterId, state: 'error', message: errorText(e) })
        },
      )
    },
    [clusterId],
  )

  // Live tail of tool events (no history replay): a finished write refreshes its parent directory (and refetches an open preview of the file), a shell result the expanded tree.
  useEffect(() => {
    const events = createFileEvents({
      root,
      isLoaded,
      refresh,
      refreshExpanded,
      written: (path) => {
        const p = previewRef.current
        if (p && p.path === path && p.state !== 'other') openFile(path)
      },
    })
    const stream = openStream(eventStreamUrl(clusterId), { eventNames: WATCHED_EVENTS, onEvent: (data) => events.handle(data) })
    return () => {
      stream.close()
      events.dispose()
    }
  }, [clusterId, root, isLoaded, refresh, refreshExpanded, openFile])

  const types = useMemo(() => {
    const out = new Map<string, FileEntry['type']>()
    for (const [dir, d] of Object.entries(state.dirs)) for (const e of d.entries) out.set(joinPath(dir, e.name), e.type)
    return out
  }, [state.dirs])

  const onItemClick = (_event: unknown, id: string) => {
    if (id.startsWith('more:')) return loadMore(id.slice('more:'.length))
    if (!id.startsWith('e:')) return
    const path = id.slice(2)
    const type = types.get(path)
    if (type === 'file' || type === 'symlink') openFile(path)
    else if (type === 'other') {
      previewToken.current += 1
      setPreview({ path, clusterId, state: 'other' })
    }
  }

  const onRefresh = () => {
    refreshExpanded()
    if (preview && preview.state !== 'other') openFile(preview.path)
  }

  const children = (dir: string): ReactNode[] => {
    const d = state.dirs[dir]
    if (!d || (dir !== '' && !state.expanded.includes(dir))) return [<TreeItem key={loadingId(dir)} itemId={loadingId(dir)} disabled label={<Row>loading</Row>} />]
    if (d.loading && d.entries.length === 0 && !d.error) return [<TreeItem key={loadingId(dir)} itemId={loadingId(dir)} disabled label={<Row>loading</Row>} />]
    const rows = d.entries.map((e) => {
      const path = joinPath(dir, e.name)
      if (e.type === 'dir') {
        const open = state.expanded.includes(path)
        return (
          <TreeItem key={entryId(path)} itemId={entryId(path)} label={<Row icon={open ? <FolderOpen fontSize="small" /> : <Folder fontSize="small" />}>{e.name}</Row>}>
            {children(path)}
          </TreeItem>
        )
      }
      const icon = e.type === 'symlink' ? <Link fontSize="small" /> : <InsertDriveFileOutlined fontSize="small" />
      return <TreeItem key={entryId(path)} itemId={entryId(path)} label={<Row icon={icon}>{e.name}</Row>} />
    })
    if (d.error && dir !== '') {
      rows.push(
        <TreeItem key={errorId(dir)} itemId={errorId(dir)} label={<Box role="alert" sx={{ color: 'error.main' }}>{d.error}</Box>} />,
      )
    }
    if (d.nextOffset !== null) rows.push(<TreeItem key={moreId(dir)} itemId={moreId(dir)} label={<Row>Load more</Row>} />)
    if (rows.length === 0 && dir !== '' && !d.loading) rows.push(<TreeItem key={emptyId(dir)} itemId={emptyId(dir)} disabled label={<Row>empty</Row>} />)
    return rows
  }

  const top = state.dirs['']
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1 }}>
        <Typography noWrap sx={{ flexGrow: 1, fontFamily: MONO_FONT, ...noteSx }}>
          {state.root ?? ''}
        </Typography>
        <IconButton size="small" aria-label="Refresh files" onClick={onRefresh}>
          <Refresh fontSize="inherit" />
        </IconButton>
      </Box>
      {top?.error && (
        <Box role="alert" sx={{ ...noteSx, color: 'error.main', px: 1 }}>
          {top.error}
        </Box>
      )}
      {state.missing && <Typography sx={{ ...noteSx, px: 1 }}>workdir not created yet</Typography>}
      <Box sx={{ flexGrow: 1, minHeight: 0, overflow: 'auto' }}>
        <SimpleTreeView
          aria-label="Files"
          expandedItems={state.expanded.map(entryId)}
          selectedItems={preview ? entryId(preview.path) : null}
          onExpandedItemsChange={(_e, ids) => setExpanded(ids.filter((i) => i.startsWith('e:')).map((i) => i.slice(2)))}
          onItemClick={onItemClick}
        >
          {top ? children('') : null}
        </SimpleTreeView>
      </Box>
      {preview && (
        <Box sx={{ maxHeight: '50%', overflow: 'auto', borderTop: `${TOKENS.borderWidth}px solid ${TOKENS.border}`, p: 1 }}>
          <Typography noWrap sx={{ ...noteSx, fontFamily: MONO_FONT, pb: 1 }}>
            {preview.path}
          </Typography>
          <PreviewBody preview={preview} />
        </Box>
      )}
    </Box>
  )
}
