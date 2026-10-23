/** Create and edit dialog for a cluster: defaults from /meta, inline validation, server 409 and 422 shown in an Alert. */
import { useEffect, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import FormControlLabel from '@mui/material/FormControlLabel'
import IconButton from '@mui/material/IconButton'
import MenuItem from '@mui/material/MenuItem'
import Switch from '@mui/material/Switch'
import TextField from '@mui/material/TextField'
import { ApiError, createCluster, getMeta, patchCluster } from '../api/client'
import type { ClusterView, Meta } from '../api/client'
import { blankValues, toSpec, validate, valuesFromSpec } from './clusterSpecForm'
import type { FormValues } from './clusterSpecForm'

export interface ClusterFormProps {
  open: boolean
  mode: 'create' | 'edit'
  /** The cluster being edited; required in edit mode. */
  cluster?: ClusterView
  onClose: () => void
  onSaved: (cluster: ClusterView) => void
}

function FormBody({ mode, cluster, onClose, onSaved }: Omit<ClusterFormProps, 'open'>) {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [values, setValues] = useState<FormValues>(() => (mode === 'edit' && cluster ? valuesFromSpec(cluster.slug, cluster.spec) : blankValues(null)))
  const [serverError, setServerError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    getMeta()
      .then((m) => {
        if (cancelled) return
        setMeta(m)
        if (mode === 'create') setValues((v) => ({ ...blankValues(m), slug: v.slug }))
      })
      .catch((e: unknown) => {
        if (!cancelled) setServerError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [mode])

  const errors = validate(values, mode, meta)
  const invalid = Object.keys(errors).length > 0
  const set = <K extends keyof FormValues>(key: K, value: FormValues[K]) => setValues((v) => ({ ...v, [key]: value }))
  const text = (key: Exclude<keyof FormValues, 'peers' | 'env'>, label: string, extra: { disabled?: boolean; multiline?: boolean } = {}) => (
    <TextField
      size="small"
      label={label}
      value={values[key]}
      onChange={(e) => set(key, e.target.value)}
      error={Boolean(errors[key]) && values[key] !== ''}
      helperText={values[key] !== '' || key === 'pvcSize' ? errors[key] : undefined}
      fullWidth
      {...extra}
    />
  )
  const setEnv = (index: number, patch: Partial<{ key: string; value: string }>) => set('env', values.env.map((r, i) => (i === index ? { ...r, ...patch } : r)))

  async function submit() {
    setBusy(true)
    setServerError(null)
    try {
      const spec = toSpec(values)
      const saved = mode === 'create' ? await createCluster({ slug: values.slug, spec }) : await patchCluster(cluster!.id, { spec })
      onSaved(saved)
    } catch (e) {
      setServerError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const withCurrent = (list: string[], current: string) => (current && !list.includes(current) ? [current, ...list] : list)
  const modelOptions = withCurrent(meta?.models ?? [], values.model)
  const effortOptions = withCurrent(meta?.efforts ?? [], values.effort)
  const dropdown = (key: 'model' | 'effort', label: string, options: string[], emptyLabel?: string) => (
    <TextField select size="small" label={label} value={values[key]} onChange={(e) => set(key, e.target.value)} fullWidth>
      {emptyLabel !== undefined && <MenuItem value="">{emptyLabel}</MenuItem>}
      {options.map((o) => (
        <MenuItem key={o} value={o}>{o}</MenuItem>
      ))}
    </TextField>
  )
  return (
    <>
      <DialogTitle>{mode === 'create' ? 'New cluster' : `Edit ${cluster?.slug ?? ''}`}</DialogTitle>
      <DialogContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          {serverError && <Alert severity="error">{serverError}</Alert>}
          {text('slug', 'Slug', { disabled: mode === 'edit' })}
          {text('replicas', 'Replicas')}
          {modelOptions.length > 0 ? dropdown('model', 'Model', modelOptions) : text('model', 'Model')}
          {effortOptions.length > 0 ? dropdown('effort', 'Effort', effortOptions, 'model default') : text('effort', 'Effort')}
          <FormControlLabel control={<Switch checked={values.peers} onChange={(e) => set('peers', e.target.checked)} />} label="Peers" />
          {text('workdir', 'Workdir')}
          {text('pvcSize', 'PVC size', { disabled: mode === 'edit' })}
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            {values.env.map((row, i) => (
              <Box key={i} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
                <TextField size="small" label={`Env key ${i + 1}`} value={row.key} onChange={(e) => setEnv(i, { key: e.target.value })} error={Boolean(errors.env?.[i])} helperText={errors.env?.[i]} fullWidth />
                <TextField size="small" label={`Env value ${i + 1}`} value={row.value} onChange={(e) => setEnv(i, { value: e.target.value })} fullWidth />
                <IconButton size="small" aria-label={`Remove env ${i + 1}`} onClick={() => set('env', values.env.filter((_, j) => j !== i))}>x</IconButton>
              </Box>
            ))}
            <Box>
              <Button size="small" onClick={() => set('env', [...values.env, { key: '', value: '' }])}>Add env</Button>
            </Box>
          </Box>
          {text('anyMcp', 'any-mcp config', { multiline: true })}
          {text('maxAttempts', 'Max attempts')}
          {text('autoCompact', 'Auto-compact percent')}
          <Box sx={{ display: 'flex', gap: 1 }}>
            {text('cpuRequest', 'CPU request')}
            {text('cpuLimit', 'CPU limit')}
          </Box>
          <Box sx={{ display: 'flex', gap: 1 }}>
            {text('memRequest', 'Memory request')}
            {text('memLimit', 'Memory limit')}
          </Box>
          {text('hookUrl', 'Pre-discard webhook URL')}
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="outlined" disabled={invalid || busy} onClick={() => void submit()}>{mode === 'create' ? 'Create' : 'Save'}</Button>
      </DialogActions>
    </>
  )
}

/**
 * @param props see ClusterFormProps.
 * @returns the dialog element; its body remounts on every open so the form starts fresh.
 */
export function ClusterForm({ open, ...rest }: ClusterFormProps) {
  return (
    <Dialog open={open} onClose={rest.onClose} fullWidth maxWidth="sm" aria-label={rest.mode === 'create' ? 'New cluster' : 'Edit cluster'}>
      <FormBody {...rest} />
    </Dialog>
  )
}
