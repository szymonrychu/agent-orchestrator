/** The left ribbon: every cluster with live state, pod counts and context gauges. */
import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { ClusterForm } from '../clusters/ClusterForm'
import { DiscardClusterDialog } from '../clusters/DiscardClusterDialog'
import { TOKENS } from '../theme'
import { ClusterRow } from './ClusterRow'
import type { ClusterView } from '../api/client'
import type { ClusterEntry } from './useClusters'

export interface RibbonProps {
  clusters: ClusterEntry[]
  selectedId: string | null
  onSelect: (id: string) => void
  error?: string | null
}

export function Ribbon({ clusters, selectedId, onSelect, error }: RibbonProps) {
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<ClusterView | null>(null)
  const [discarding, setDiscarding] = useState<ClusterView | null>(null)
  return (
    <Box component="nav" aria-label="Clusters" sx={{ display: 'flex', flexDirection: 'column' }}>
      <Box sx={{ p: 2 }}>
        <Button variant="outlined" size="small" fullWidth onClick={() => setCreating(true)}>New cluster</Button>
      </Box>
      {error && (
        <Box role="alert" sx={{ p: 2, fontSize: TOKENS.fontSizes.sm, color: 'error.main' }}>
          {error}
        </Box>
      )}
      {clusters.length === 0 && !error && (
        <Box sx={{ p: 2, fontSize: TOKENS.fontSizes.sm, color: 'text.secondary' }}>No clusters yet</Box>
      )}
      {clusters.map((entry) => (
        <ClusterRow key={entry.cluster.id} entry={entry} selected={entry.cluster.id === selectedId} onSelect={() => onSelect(entry.cluster.id)} onEdit={() => setEditing(entry.cluster)} onDiscard={() => setDiscarding(entry.cluster)} />
      ))}
      <ClusterForm open={creating} mode="create" onClose={() => setCreating(false)} onSaved={(c) => { setCreating(false); onSelect(c.id) }} />
      {editing && <ClusterForm open mode="edit" cluster={editing} onClose={() => setEditing(null)} onSaved={() => setEditing(null)} />}
      {discarding && <DiscardClusterDialog open cluster={discarding} onClose={() => setDiscarding(null)} onDiscarded={() => setDiscarding(null)} />}
    </Box>
  )
}
