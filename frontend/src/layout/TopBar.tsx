/** Top bar: hamburger menu, one tab per cluster (state mark, slug with its preset name, context gauge) and a New cluster button. */
import { useState } from 'react'
import AddIcon from '@mui/icons-material/Add'
import MenuIcon from '@mui/icons-material/Menu'
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutlineOutlined'
import MoreVertIcon from '@mui/icons-material/MoreVert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { ContextGauge } from '../ribbon/ContextGauge'
import { StateMark } from '../ribbon/stateMark'
import type { ClusterEntry } from '../ribbon/useClusters'
import type { ClusterView } from '../api/client'
import { TOKENS } from '../theme'
import { SessionsMenu } from './SessionsMenu'
import type { SettingsCategory } from './SessionsMenu'

export interface TopBarProps {
  /** Clusters in display order (the caller passes `sortedClusters`). */
  clusters: ClusterEntry[]
  selectedId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  onDiscard: (cluster: ClusterView) => void
  settingsCategories: SettingsCategory[]
}

interface ActionsMenu {
  anchor: HTMLElement
  cluster: ClusterView
}

export function TopBar({ clusters, selectedId, onSelect, onNew, onDiscard, settingsCategories }: TopBarProps) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [actions, setActions] = useState<ActionsMenu | null>(null)
  // MUI Tabs needs a value that matches a tab, or false when none is selected.
  const value = clusters.some((e) => e.cluster.id === selectedId) ? selectedId : false
  return (
    <Box component="header" sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1, borderBottom: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
      <IconButton aria-label="Open menu" onClick={() => setMenuOpen(true)}>
        <MenuIcon />
      </IconButton>
      <Tabs value={value} variant="scrollable" scrollButtons="auto" aria-label="Clusters" sx={{ flexGrow: 1, minWidth: 0 }}>
        {clusters.map(({ cluster }) => (
          <Tab
            key={cluster.id}
            component="div"
            value={cluster.id}
            onClick={() => onSelect(cluster.id)}
            label={
              <Box component="span" sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <StateMark state={cluster.summary.state} />
                <Box component="span" sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', textAlign: 'left' }}>
                  <Box component="span" sx={{ fontWeight: TOKENS.fontWeights.medium }}>{cluster.slug}</Box>
                  {cluster.preset_name ? <Typography variant="caption" color="text.secondary" noWrap sx={{ maxWidth: 160, textTransform: 'none' }}>{cluster.preset_name}</Typography> : null}
                </Box>
                {cluster.status.discard_hook_failed ? (
                  <Tooltip title="discard hook failed">
                    <ErrorOutlineIcon fontSize="small" color="error" role="img" aria-label="discard hook failed" />
                  </Tooltip>
                ) : null}
                <Tooltip title={`${cluster.summary.ready} of ${cluster.summary.replicas} pods ready`}>
                  <Box component="span" sx={{ fontSize: `${TOKENS.fontSizes.sm / 16}rem`, color: 'text.secondary' }}>{`${cluster.summary.ready}/${cluster.summary.replicas}`}</Box>
                </Tooltip>
                <ContextGauge value={cluster.summary.context_pct_max} autoCompactPct={cluster.summary.auto_compact_pct} />
                <IconButton
                  size="small"
                  component="span"
                  role="button"
                  aria-haspopup="menu"
                  aria-label="Cluster actions"
                  onClick={(e) => { e.stopPropagation(); setActions({ anchor: e.currentTarget, cluster }) }}
                >
                  <MoreVertIcon fontSize="small" />
                </IconButton>
              </Box>
            }
          />
        ))}
      </Tabs>
      <Button variant="outlined" size="small" startIcon={<AddIcon />} onClick={onNew} sx={{ flexShrink: 0 }}>New cluster</Button>
      <Menu anchorEl={actions?.anchor ?? null} open={actions !== null} onClose={() => setActions(null)}>
        <MenuItem onClick={() => { const target = actions?.cluster; setActions(null); if (target) onDiscard(target) }}>Discard</MenuItem>
      </Menu>
      <SessionsMenu open={menuOpen} onClose={() => setMenuOpen(false)} clusters={clusters} selectedId={selectedId} onSelect={onSelect} settingsCategories={settingsCategories} />
    </Box>
  )
}
