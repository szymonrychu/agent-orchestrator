/** Discard confirmation: states exactly what is deleted, names the pre-discard hook, requires the typed slug. */
import { useEffect, useState } from 'react'
import Alert from '@mui/material/Alert'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { deleteCluster, getMeta } from '../api/client'
import type { ClusterView, Meta } from '../api/client'
import { ConfirmDialog } from '../layout/ConfirmDialog'

/** Mount it per discard (state starts fresh); the typed slug is not reset between opens. */
export interface DiscardClusterDialogProps {
  open: boolean
  cluster: ClusterView
  onClose: () => void
  onDiscarded: () => void
}

/** What the pre-discard export goes to, or null when no hook applies. */
export function hookTarget(cluster: ClusterView, meta: Meta | null): string | null {
  if (cluster.spec.discard_hook_url) return cluster.spec.discard_hook_url
  return meta && meta.discard_hook !== 'none' ? `the configured ${meta.discard_hook} hook` : null
}

export function DiscardClusterDialog({ open, cluster, onClose, onDiscarded }: DiscardClusterDialogProps) {
  const [typed, setTyped] = useState('')
  const [meta, setMeta] = useState<Meta | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    getMeta()
      .then((m) => {
        if (!cancelled) setMeta(m)
      })
      .catch(() => {
        // The cluster's own hook URL still applies without meta.
      })
    return () => {
      cancelled = true
    }
  }, [open])

  const target = hookTarget(cluster, meta)
  const hookFailed = Boolean(cluster.status.discard_hook_failed)

  async function discard() {
    setError(null)
    try {
      await deleteCluster(cluster.id)
      onDiscarded()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title={`Discard ${cluster.slug}?`}
      body="Everything is deleted: the agents, the workspace PVC and its files, and the cluster's whole chat history (messages, events, turns, commands, checks). There is no undo."
      confirmLabel="Discard"
      confirmDisabled={typed !== cluster.slug}
      onConfirm={() => void discard()}
      onCancel={onClose}
    >
      {target && (
        <Typography sx={{ mt: 2 }}>
          The full history is exported to {target} first; a failing hook holds the discard.
        </Typography>
      )}
      {hookFailed && <Alert severity="error" sx={{ mt: 2 }}>The pre-discard hook failed, so the discard is on hold.</Alert>}
      {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
      <TextField size="small" fullWidth sx={{ mt: 2 }} label="Type the slug to confirm" value={typed} onChange={(e) => setTyped(e.target.value)} />
    </ConfirmDialog>
  )
}
