/** Cluster init scripts: how the cluster was initialised (run state, log tail, paths) and an editor for the next cluster. */
import { useEffect, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import IconButton from '@mui/material/IconButton'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { getInitScripts, putInitScripts } from '../api/client'
import type { ClusterView, InitScriptsView } from '../api/client'
import { TOKENS } from '../theme'
import { initScriptErrors } from './clusterSpecForm'

export interface InitScriptsDialogProps {
  open: boolean
  cluster: ClusterView
  onClose: () => void
}

function RunState({ status }: { status: InitScriptsView['status'] }) {
  if (!status) return <Typography color="text.secondary">No run recorded for this cluster.</Typography>
  const detail = [
    status.exit_code != null ? `exit code ${status.exit_code}` : null,
    status.reason ? `reason: ${status.reason}` : null,
    status.started_at ? `started ${status.started_at}` : null,
    status.finished_at ? `finished ${status.finished_at}` : null,
  ].filter(Boolean)
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <Typography component="span" sx={{ fontWeight: TOKENS.fontWeights.medium, color: status.state === 'failed' ? 'error.main' : 'text.primary' }}>{status.state}</Typography>
        {detail.length > 0 && <Typography component="span" variant="body2" color="text.secondary">{detail.join(', ')}</Typography>}
      </Box>
      {status.log_tail ? (
        <Box component="pre" aria-label="Init log tail" sx={{ m: 0, p: 1, maxHeight: 240, overflow: 'auto', fontFamily: 'monospace', fontSize: `${TOKENS.fontSizes.sm / 16}rem`, border: `${TOKENS.borderWidth}px solid ${TOKENS.border}`, whiteSpace: 'pre-wrap' }}>{status.log_tail}</Box>
      ) : null}
    </Box>
  )
}

export function InitScriptsDialog({ open, cluster, onClose }: InitScriptsDialogProps) {
  const [view, setView] = useState<InitScriptsView | null>(null)
  const [scripts, setScripts] = useState<string[]>([])
  const [errors, setErrors] = useState<(string | undefined)[] | undefined>()
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<InitScriptsView | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    getInitScripts(cluster.id)
      .then((v) => {
        if (cancelled) return
        setView(v)
        setScripts([...v.scripts])
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [open, cluster.id])

  const change = (next: string[]) => {
    setScripts(next)
    setErrors(undefined)
    setSaved(null)
  }

  async function save() {
    const found = initScriptErrors(scripts)
    setErrors(found)
    if (found) return
    setError(null)
    setSaving(true)
    try {
      const v = await putInitScripts(cluster.id, scripts)
      setView(v)
      setScripts([...v.scripts])
      setSaved(v)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>Init scripts - {cluster.slug}</DialogTitle>
      <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {error && <Alert severity="error">{error}</Alert>}
        {view && (
          <>
            <RunState status={view.status} />
            <Typography variant="body2" color="text.secondary">
              Scripts in the workdir: <Box component="span" sx={{ fontFamily: 'monospace' }}>{view.script_path}</Box>
            </Typography>
            {view.edited_since_run && <Alert severity="info">The scripts were edited since the run; the log above shows the earlier version.</Alert>}
            {scripts.map((script, i) => (
              <Box key={i} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
                <TextField
                  size="small"
                  label={`Init script ${i + 1}`}
                  value={script}
                  onChange={(e) => change(scripts.map((s, j) => (j === i ? e.target.value : s)))}
                  error={Boolean(errors?.[i])}
                  helperText={errors?.[i]}
                  multiline
                  minRows={3}
                  fullWidth
                  slotProps={{ input: { sx: { fontFamily: 'monospace' } } }}
                />
                <IconButton size="small" aria-label={`Remove init script ${i + 1}`} onClick={() => change(scripts.filter((_, j) => j !== i))}>x</IconButton>
              </Box>
            ))}
            <Box>
              <Button size="small" onClick={() => change([...scripts, ''])}>Add init script</Button>
            </Box>
            {saved && (
              <Alert severity="success">
                <div>Saved. Applies to the next cluster</div>
                {saved.preset_updated && saved.preset_name && <div>Preset {saved.preset_name} updated</div>}
              </Alert>
            )}
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
        <Button variant="contained" disabled={!view || saving} onClick={() => void save()}>Save</Button>
      </DialogActions>
    </Dialog>
  )
}
