/** Grow or shrink a cluster within its cap; the count follows the status stream, never an optimistic update. */
import { useState } from 'react'
import AddIcon from '@mui/icons-material/Add'
import RemoveIcon from '@mui/icons-material/Remove'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { ApiError, scaleCluster, type ClusterView } from '../api/client'
import { TOKENS } from '../theme'

export function ScaleControl({ cluster }: { cluster: ClusterView }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ clusterId: string; text: string } | null>(null)
  const replicas = cluster.spec.replicas
  const max = cluster.max_replicas
  if (max <= 1) return null
  const isStatic = cluster.static
  const go = async (target: number) => {
    setBusy(true)
    setError(null)
    try {
      await scaleCluster(cluster.id, { replicas: target })
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      const hint = e instanceof ApiError && e.code === 'leader_would_leave' ? ' Make another agent leader first.' : ''
      setError({ clusterId: cluster.id, text: msg + hint })
    } finally {
      setBusy(false)
    }
  }
  const buttons = (
    <Box component="span" sx={{ display: 'inline-flex' }}>
      <IconButton size="small" aria-label="Remove an agent" disabled={isStatic || busy || replicas <= 1} onClick={() => go(replicas - 1)}>
        <RemoveIcon fontSize="small" />
      </IconButton>
      <IconButton size="small" aria-label="Add an agent" disabled={isStatic || busy || replicas >= max} onClick={() => go(replicas + 1)}>
        <AddIcon fontSize="small" />
      </IconButton>
    </Box>
  )
  const shownError = error && error.clusterId === cluster.id ? error.text : null
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Typography sx={{ fontSize: `${TOKENS.fontSizes.sm / 16}rem`, color: 'text.secondary' }}>{`${replicas} of ${max} agents`}</Typography>
        {isStatic ? <Tooltip title="Static cluster: scaling is off">{buttons}</Tooltip> : buttons}
      </Box>
      {shownError ? (
        <Typography role="alert" sx={{ fontSize: `${TOKENS.fontSizes.xs / 16}rem`, color: 'error.main' }}>
          {shownError}
        </Typography>
      ) : null}
    </Box>
  )
}
