/** One cluster of the ribbon: state mark, slug, ready/replicas, gauge, expand toggle and, expanded, its agents. */
import { useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import IconButton from '@mui/material/IconButton'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import { StatusItem } from '../components/StatusItem'
import { TOKENS } from '../theme'
import { AgentRow } from './AgentRow'
import { ContextGauge } from './ContextGauge'
import { StateMark } from './stateMark'
import { sortedAgents } from './useClusters'
import type { ClusterEntry } from './useClusters'

export interface ClusterRowProps {
  entry: ClusterEntry
  selected: boolean
  onSelect: () => void
  onEdit: () => void
  onDiscard: () => void
}

export function ClusterRow({ entry, selected, onSelect, onEdit, onDiscard }: ClusterRowProps) {
  const [open, setOpen] = useState(false)
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)
  const { cluster } = entry
  const { summary, slug } = cluster
  const agents = sortedAgents(entry)
  return (
    <Box data-testid={`cluster-row-${slug}`} aria-current={selected ? 'true' : undefined} sx={{ borderBottom: `${TOKENS.borderWidth}px solid ${TOKENS.border}`, bgcolor: selected ? TOKENS.bgRaised : 'transparent' }}>
      <Box data-testid={`cluster-head-${slug}`} sx={{ display: 'flex', alignItems: 'center', pr: 1 }}>
        <ButtonBase aria-label={`select ${slug}`} onClick={onSelect} sx={{ flexGrow: 1, minWidth: 0, display: 'flex', alignItems: 'center', justifyContent: 'flex-start', gap: 1, px: 2, py: 1, textAlign: 'left' }}>
          <StateMark state={summary.state} />
          <Box component="span" sx={{ flexGrow: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: TOKENS.fontWeights.medium }}>
            {slug}
          </Box>
          {cluster.status.discard_hook_failed ? <StatusItem label="" value="hook failed" tooltip="The pre-discard hook failed, the discard is on hold" /> : null}
          <StatusItem label="" value={`${summary.ready}/${summary.replicas}`} tooltip={`${summary.ready} of ${summary.replicas} pods ready`} />
          <ContextGauge value={summary.context_pct_max} autoCompactPct={summary.auto_compact_pct} />
        </ButtonBase>
        <IconButton size="small" aria-expanded={open} aria-label={`${open ? 'collapse' : 'expand'} ${slug}`} onClick={() => setOpen((o) => !o)}>
          {open ? '-' : '+'}
        </IconButton>
        <IconButton size="small" aria-haspopup="menu" aria-label={`actions ${slug}`} onClick={(e) => setMenuAnchor(e.currentTarget)}>
          ...
        </IconButton>
        <Menu anchorEl={menuAnchor} open={menuAnchor !== null} onClose={() => setMenuAnchor(null)}>
          <MenuItem onClick={() => { setMenuAnchor(null); onEdit() }}>Edit</MenuItem>
          <MenuItem onClick={() => { setMenuAnchor(null); onDiscard() }}>Discard</MenuItem>
        </Menu>
      </Box>
      {open && agents.map((a) => <AgentRow key={a.id} agent={a} autoCompactPct={summary.auto_compact_pct} />)}
    </Box>
  )
}
