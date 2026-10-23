/** The left ribbon: every cluster with live state, pod counts and context gauges. */
import Box from '@mui/material/Box'
import { TOKENS } from '../theme'
import { ClusterRow } from './ClusterRow'
import type { ClusterEntry } from './useClusters'

export interface RibbonProps {
  clusters: ClusterEntry[]
  selectedId: string | null
  onSelect: (id: string) => void
  error?: string | null
}

export function Ribbon({ clusters, selectedId, onSelect, error }: RibbonProps) {
  return (
    <Box component="nav" aria-label="Clusters" sx={{ display: 'flex', flexDirection: 'column' }}>
      {error && (
        <Box role="alert" sx={{ p: 2, fontSize: TOKENS.fontSizes.sm, color: 'error.main' }}>
          {error}
        </Box>
      )}
      {clusters.length === 0 && !error && (
        <Box sx={{ p: 2, fontSize: TOKENS.fontSizes.sm, color: 'text.secondary' }}>No clusters yet</Box>
      )}
      {clusters.map((entry) => (
        <ClusterRow key={entry.cluster.id} entry={entry} selected={entry.cluster.id === selectedId} onSelect={() => onSelect(entry.cluster.id)} />
      ))}
    </Box>
  )
}
