/** New cluster dialog: choose a preset and optionally name the cluster; the server snapshots the preset's spec. */
import { useEffect, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import { createCluster, listPresets } from '../api/client'
import type { ClusterView, CreateCluster, PresetView } from '../api/client'
import { MAX_SLUG } from './clusterSpecForm'

export interface NewClusterDialogProps {
  open: boolean
  onClose: () => void
  onCreated: (cluster: ClusterView) => void
}

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e))

function DialogBody({ onClose, onCreated }: Omit<NewClusterDialogProps, 'open'>) {
  const [presets, setPresets] = useState<PresetView[]>([])
  const [presetId, setPresetId] = useState('')
  const [slug, setSlug] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    listPresets()
      .then((list) => {
        if (cancelled) return
        setPresets(list)
        setPresetId((list.find((p) => p.is_default) ?? list[0])?.id ?? '')
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(message(e))
      })
    return () => {
      cancelled = true
    }
  }, [])

  const slugTooLong = slug.length > MAX_SLUG

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const body: CreateCluster = slug === '' ? { preset_id: presetId } : { preset_id: presetId, slug }
      onCreated(await createCluster(body))
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <DialogTitle>New cluster</DialogTitle>
      <DialogContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          {error && <Alert severity="error" variant="outlined">{error}</Alert>}
          <TextField select size="small" label="Preset" value={presetId} onChange={(e) => setPresetId(e.target.value)} fullWidth>
            {presets.map((p) => (
              <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>
            ))}
          </TextField>
          <TextField
            size="small"
            label="Slug"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            error={slugTooLong}
            helperText={slugTooLong ? `Slug must be at most ${MAX_SLUG} characters` : 'An empty slug is generated from the preset name'}
            fullWidth
          />
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="outlined" disabled={presetId === '' || slugTooLong || busy} onClick={() => void submit()}>Create</Button>
      </DialogActions>
    </>
  )
}

/**
 * @param props see NewClusterDialogProps.
 * @returns the dialog element; its body remounts on every open so it reloads the presets and starts fresh.
 */
export function NewClusterDialog({ open, ...rest }: NewClusterDialogProps) {
  return (
    <Dialog open={open} onClose={rest.onClose} fullWidth maxWidth="xs" aria-label="New cluster">
      <DialogBody {...rest} />
    </Dialog>
  )
}
