/** The three-panel workspace of one cluster: files, chat and terminal side by side with draggable separators. */
import CloseFullscreen from '@mui/icons-material/CloseFullscreen'
import FilterNone from '@mui/icons-material/FilterNone'
import Minimize from '@mui/icons-material/Minimize'
import OpenInFull from '@mui/icons-material/OpenInFull'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import { styled } from '@mui/material/styles'
import ToggleButton from '@mui/material/ToggleButton'
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup'
import Typography from '@mui/material/Typography'
import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Group, type GroupImperativeHandle, type Layout, Panel, type PanelImperativeHandle, Separator } from 'react-resizable-panels'
import { TOKENS } from '../theme'
import {
  PANELS, type PanelLayout, type PanelName, maximize, minimize, normalisedSizes, parseLayout, reset, resize, restore, restoreMaximized, serialiseLayout,
} from './panelLayout'
import { loadJson, saveJson } from './storage'
import { useLayout } from './useLayout'

export interface WorkspaceProps {
  clusterId: string
  files: ReactNode
  chat: ReactNode
  terminal: ReactNode
}

const TITLES: Record<PanelName, string> = { files: 'Files', chat: 'Chat', terminal: 'Terminal' }
/** Width in pixels of a minimized panel's strip. */
const STRIP_WIDTH = 40

const layoutKey = (clusterId: string) => `layout:${clusterId}`

/** The library's inline `flex-basis: auto` beats a stylesheet flex shorthand, so the size is also passed as an inline style. */
const DIVIDER_WIDTH = 2

const Divider = styled(Separator)(({ theme }) => ({
  flex: `0 0 ${DIVIDER_WIDTH}px`,
  backgroundColor: TOKENS.borderStrong,
  '&[data-separator="hover"], &[data-separator="active"], &:focus-visible': { backgroundColor: theme.palette.primary.main, outline: 'none' },
}))

interface PanelFrameProps {
  name: PanelName
  layout: PanelLayout
  onLayout: (next: PanelLayout) => void
  children: ReactNode
}

/** Header (name, minimize and maximize or restore) over the slot, or the strip while minimized. The slot stays mounted either way. */
function PanelFrame({ name, layout, onLayout, children }: PanelFrameProps) {
  const title = TITLES[name]
  const collapsed = layout.collapsed.has(name)
  const sole = !collapsed && layout.collapsed.size === PANELS.length - 1
  return (
    <Box sx={{ display: 'flex', height: '100%', minWidth: 0 }}>
      {collapsed && (
        <Box data-testid={`strip-${name}`} sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: '100%', py: 1, gap: 1, bgcolor: 'background.paper' }}>
          <IconButton size="small" aria-label={`Restore ${title}`} onClick={() => onLayout(restore(layout, name))}>
            <FilterNone fontSize="inherit" />
          </IconButton>
          <Typography variant="caption" sx={{ writingMode: 'vertical-rl', color: 'text.secondary' }}>
            {title}
          </Typography>
        </Box>
      )}
      <Box hidden={collapsed} sx={{ display: collapsed ? 'none' : 'flex', flexDirection: 'column', flexGrow: 1, minWidth: 0, minHeight: 0 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', px: 1, borderBottom: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
          <Typography variant="caption" sx={{ flexGrow: 1, color: 'text.secondary' }}>
            {title}
          </Typography>
          {layout.before ? (
            <IconButton size="small" aria-label={`Restore ${title}`} onClick={() => onLayout(restoreMaximized(layout))}>
              <CloseFullscreen fontSize="inherit" />
            </IconButton>
          ) : (
            <>
              <IconButton size="small" aria-label={`Minimize ${title}`} disabled={sole} onClick={() => onLayout(minimize(layout, name))}>
                <Minimize fontSize="inherit" />
              </IconButton>
              {!sole && (
                <IconButton size="small" aria-label={`Maximize ${title}`} onClick={() => onLayout(maximize(layout, name))}>
                  <OpenInFull fontSize="inherit" />
                </IconButton>
              )}
            </>
          )}
        </Box>
        <Box sx={{ flexGrow: 1, minHeight: 0, minWidth: 0, overflow: 'auto' }}>{children}</Box>
      </Box>
    </Box>
  )
}

function Phone({ slots }: { slots: Record<PanelName, ReactNode> }) {
  const [shown, setShown] = useState<PanelName>('chat')
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <ToggleButtonGroup
        exclusive
        fullWidth
        size="small"
        aria-label="Panel"
        value={shown}
        onChange={(_e, next: PanelName | null) => next && setShown(next)}
        sx={{ pb: 1 }}
      >
        {PANELS.map((name) => (
          <ToggleButton key={name} value={name}>
            {TITLES[name]}
          </ToggleButton>
        ))}
      </ToggleButtonGroup>
      {PANELS.map((name) => (
        <Box key={name} hidden={name !== shown} sx={{ display: name === shown ? 'block' : 'none', flexGrow: 1, minHeight: 0, overflow: 'auto' }}>
          {slots[name]}
        </Box>
      ))}
    </Box>
  )
}

function Desktop({ layout, onLayout, slots }: { layout: PanelLayout; onLayout: (next: PanelLayout) => void; slots: Record<PanelName, ReactNode> }) {
  const group = useRef<GroupImperativeHandle | null>(null)
  const panels = useRef<Record<PanelName, PanelImperativeHandle | null>>({ files: null, chat: null, terminal: null })
  const [initial] = useState(() => normalisedSizes(layout.sizes))

  // Drive the library from the layout state: collapse and expand to match, then snap to the stored sizes when nothing is minimized.
  useLayoutEffect(() => {
    for (const name of PANELS) {
      const panel = panels.current[name]
      if (!panel) continue
      const want = layout.collapsed.has(name)
      if (want && !panel.isCollapsed()) panel.collapse()
      else if (!want && panel.isCollapsed()) panel.expand()
    }
    if (layout.collapsed.size === 0 && group.current) {
      const target = Object.fromEntries(PANELS.map((name, i) => [name, normalisedSizes(layout.sizes)[i]]))
      const current = group.current.getLayout()
      if (PANELS.some((name) => Math.abs((current[name] ?? 0) - target[name]) > 0.01)) group.current.setLayout(target)
    }
  }, [layout])

  // A drag changes sizes and may collapse a panel; adopt what the library ended up with.
  const onLayoutChanged = (next: Layout, meta: { isUserInteraction: boolean }) => {
    if (!meta.isUserInteraction) return
    const collapsed = PANELS.filter((name) => panels.current[name]?.isCollapsed())
    const sizes = PANELS.map((name) => (collapsed.includes(name) ? null : (next[name] ?? null)))
    onLayout(resize(layout, sizes, collapsed))
  }

  return (
    <Group orientation="horizontal" groupRef={group} onLayoutChanged={onLayoutChanged} style={{ height: '100%' }}>
      {PANELS.flatMap((name, i) => [
        ...(i > 0 ? [<Divider key={`sep-${name}`} style={{ flexBasis: DIVIDER_WIDTH }} disableDoubleClick onDoubleClick={() => onLayout(reset())} />] : []),
        <Panel
          key={name}
          id={name}
          data-collapsed={layout.collapsed.has(name)}
          panelRef={(handle) => {
            panels.current[name] = handle
          }}
          collapsible
          collapsedSize={STRIP_WIDTH}
          minSize="10"
          defaultSize={`${initial[i]}`}
        >
          <PanelFrame name={name} layout={layout} onLayout={onLayout}>
            {slots[name]}
          </PanelFrame>
        </Panel>,
      ])}
    </Group>
  )
}

function ClusterWorkspace({ clusterId, files, chat, terminal }: WorkspaceProps) {
  const key = layoutKey(clusterId)
  const [layout, setLayout] = useState<PanelLayout>(() => parseLayout(loadJson<unknown>(key, null)))
  const layoutClass = useLayout()
  useEffect(() => saveJson(key, serialiseLayout(layout)), [key, layout])
  const slots = { files, chat, terminal }
  return (
    <Box data-testid="workspace" data-cluster={clusterId} sx={{ height: '100%', minHeight: 0 }}>
      {layoutClass === 'phone' ? <Phone slots={slots} /> : <Desktop layout={layout} onLayout={setLayout} slots={slots} />}
    </Box>
  )
}

/**
 * The workspace of one cluster. Its layout is kept per cluster in the browser and restored when the cluster is shown again.
 * @param clusterId - the cluster whose layout is stored under `orch:layout:<clusterId>`.
 * @param files - slot for the filesystem panel.
 * @param chat - slot for the chat panel.
 * @param terminal - slot for the terminal panel.
 */
export function Workspace(props: WorkspaceProps) {
  return <ClusterWorkspace key={props.clusterId} {...props} />
}
