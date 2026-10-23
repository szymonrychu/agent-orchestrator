/** Hamburger drawer: every session (cluster) from the top, a settings section pinned to the bottom. */
import Box from '@mui/material/Box'
import Drawer from '@mui/material/Drawer'
import List from '@mui/material/List'
import ListItem from '@mui/material/ListItem'
import ListItemButton from '@mui/material/ListItemButton'
import ListItemText from '@mui/material/ListItemText'
import Typography from '@mui/material/Typography'
import { StateMark } from '../ribbon/stateMark'
import type { ClusterEntry } from '../ribbon/useClusters'
import { TOKENS } from '../theme'

/** One entry of the Settings section; other epics add theirs (for example Presets). */
export interface SettingsCategory {
  id: string
  label: string
  onOpen: () => void
}

export interface SessionsMenuProps {
  open: boolean
  onClose: () => void
  clusters: ClusterEntry[]
  selectedId: string | null
  onSelect: (id: string) => void
  settingsCategories: SettingsCategory[]
}

const DRAWER_WIDTH_PX = 280

export function SessionsMenu({ open, onClose, clusters, selectedId, onSelect, settingsCategories }: SessionsMenuProps) {
  return (
    <Drawer anchor="left" variant="temporary" open={open} onClose={onClose} slotProps={{ paper: { sx: { width: DRAWER_WIDTH_PX, display: 'flex', flexDirection: 'column' } } }}>
      <Typography component="h2" sx={{ px: 2, py: 1, fontSize: TOKENS.fontSizes.sm, color: 'text.secondary' }}>Sessions</Typography>
      <List aria-label="Sessions" dense disablePadding>
        {clusters.length === 0 && <Typography sx={{ px: 2, py: 1, fontSize: TOKENS.fontSizes.sm, color: 'text.secondary' }}>No clusters yet</Typography>}
        {clusters.map(({ cluster }) => (
          <ListItem key={cluster.id} disablePadding>
            <ListItemButton selected={cluster.id === selectedId} aria-current={cluster.id === selectedId ? 'true' : undefined} onClick={() => { onSelect(cluster.id); onClose() }} sx={{ gap: 1 }}>
              <StateMark state={cluster.summary.state} />
              <ListItemText primary={cluster.slug} />
            </ListItemButton>
          </ListItem>
        ))}
      </List>
      <Box component="section" aria-label="Settings" sx={{ mt: 'auto', borderTop: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
        <Typography component="h2" sx={{ px: 2, py: 1, fontSize: TOKENS.fontSizes.sm, color: 'text.secondary' }}>Settings</Typography>
        {settingsCategories.length === 0 ? (
          <Typography sx={{ px: 2, pb: 2, fontSize: TOKENS.fontSizes.sm, color: 'text.secondary' }}>No settings yet</Typography>
        ) : (
          <List dense disablePadding>
            {settingsCategories.map((c) => (
              <ListItem key={c.id} disablePadding>
                <ListItemButton onClick={() => { c.onOpen(); onClose() }}>
                  <ListItemText primary={c.label} />
                </ListItemButton>
              </ListItem>
            ))}
          </List>
        )}
      </Box>
    </Drawer>
  )
}
