/**
 * Application shell. Tablet and desktop: a permanent left aside (docked like the ros2 MapSidebar) beside the main pane.
 * Phone: a dense top bar with a menu button opening the aside in a temporary Drawer.
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import AppBar from '@mui/material/AppBar'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Drawer from '@mui/material/Drawer'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import { PANEL_BG, TOKENS } from '../theme'
import { useLayout } from './useLayout'

/** Width of the docked aside. */
export const ASIDE_WIDTH = 'clamp(240px, 24vw, 340px)'

export interface ShellProps {
  title: string
  /** Aside content; `close` dismisses the phone drawer (a no-op when docked). */
  aside: (close: () => void) => ReactNode
  children?: ReactNode
}

export function Shell({ title, aside, children }: ShellProps) {
  const layout = useLayout()
  const [open, setOpen] = useState(false)
  const close = () => setOpen(false)
  const header = (
    <Typography variant="subtitle2" component="h1" sx={{ px: 2, height: TOKENS.panelHeaderPx, display: 'flex', alignItems: 'center', borderBottom: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
      {title}
    </Typography>
  )
  const main = (
    <Box component="main" sx={{ flexGrow: 1, minWidth: 0, minHeight: 0, overflow: 'auto', p: 2 }}>
      {children}
    </Box>
  )

  if (layout === 'phone') {
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', height: '100dvh' }}>
        <AppBar position="static">
          <Toolbar variant="dense" disableGutters sx={{ gap: 1, px: 1 }}>
            <Button aria-label="open clusters" onClick={() => setOpen(true)}>
              Menu
            </Button>
            <Typography variant="subtitle2" component="h1">
              {title}
            </Typography>
          </Toolbar>
        </AppBar>
        <Drawer variant="temporary" open={open} onClose={close} data-testid="ribbon-drawer" slotProps={{ paper: { sx: { width: ASIDE_WIDTH, maxWidth: '85vw', bgcolor: PANEL_BG } } }}>
          {header}
          {aside(close)}
        </Drawer>
        {main}
      </Box>
    )
  }

  return (
    <Box sx={{ display: 'flex', height: '100dvh' }}>
      <Box
        component="aside"
        aria-label="Clusters"
        data-testid="ribbon-aside"
        sx={{ width: ASIDE_WIDTH, height: '100%', flexShrink: 0, overflowY: 'auto', bgcolor: PANEL_BG, borderRight: `${TOKENS.borderWidth}px solid ${TOKENS.border}`, boxSizing: 'border-box' }}
      >
        {header}
        {aside(() => {})}
      </Box>
      {main}
    </Box>
  )
}
