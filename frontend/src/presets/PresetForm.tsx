/** Preset editor (new, edit, copy): /meta defaults for unset fields, sparse saving, inline validation, server 409 and 422 shown in an Alert. */
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
import { createPreset, getMeta, updatePreset } from '../api/client'
import type { ClusterSpec, Meta, PresetView } from '../api/client'
import { copyName, setKeyOf, stateFromPreset, toPresetSpec, validate } from '../clusters/clusterSpecForm'
import type { FormState, FormValues } from '../clusters/clusterSpecForm'

export interface PresetFormProps {
  open: boolean
  /** The preset being edited (edit mode). */
  preset?: PresetView
  /** The preset being copied (copy mode); saved as a new preset. */
  copyOf?: PresetView
  onClose: () => void
  onSaved: (preset: PresetView) => void
}

const NOT_AVAILABLE = ' (not available)'

function initialState(meta: Meta | null, preset?: PresetView, copyOf?: PresetView): FormState {
  const stored = (p?: PresetView) => (p?.spec ?? {}) as Partial<ClusterSpec>
  if (preset) return stateFromPreset(meta, preset.name, stored(preset))
  if (copyOf) return stateFromPreset(meta, copyName(copyOf.name), stored(copyOf))
  return stateFromPreset(meta, '', {})
}

function FormBody({ preset, copyOf, onClose, onSaved }: Omit<PresetFormProps, 'open'>) {
  const [source] = useState({ preset, copyOf })
  const [meta, setMeta] = useState<Meta | null>(null)
  const [state, setState] = useState<FormState>(() => initialState(null, preset, copyOf))
  const [serverError, setServerError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const { values, setKeys } = state
  const editing = preset !== undefined

  useEffect(() => {
    let cancelled = false
    getMeta()
      .then((m) => {
        if (cancelled) return
        setMeta(m)
        setState((s) => {
          const fresh = initialState(m, source.preset, source.copyOf)
          return { ...fresh, values: { ...fresh.values, name: s.values.name } }
        })
      })
      .catch((e: unknown) => {
        if (!cancelled) setServerError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [source])

  const errors = validate(values, meta)
  const invalid = Object.keys(errors).length > 0
  const set = <K extends keyof FormValues>(key: K, value: FormValues[K]) =>
    setState((s) => {
      const setKey = setKeyOf(key)
      return { values: { ...s.values, [key]: value }, setKeys: setKey ? new Set(s.setKeys).add(setKey) : s.setKeys }
    })
  const text = (key: Exclude<keyof FormValues, 'peers' | 'env'>, label: string, extra: { disabled?: boolean; multiline?: boolean } = {}) => (
    <TextField
      size="small"
      label={label}
      value={values[key]}
      onChange={(e) => set(key, e.target.value)}
      error={Boolean(errors[key]) && values[key] !== ''}
      helperText={values[key] !== '' ? errors[key] : undefined}
      fullWidth
      {...extra}
    />
  )
  const setEnv = (index: number, patch: Partial<{ key: string; value: string }>) => set('env', values.env.map((r, i) => (i === index ? { ...r, ...patch } : r)))

  async function submit() {
    setBusy(true)
    setServerError(null)
    try {
      const body = { name: values.name, spec: toPresetSpec(values, setKeys) as ClusterSpec }
      onSaved(preset ? await updatePreset(preset.id, body) : await createPreset(body))
    } catch (e) {
      setServerError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const dropdown = (key: 'model' | 'effort', label: string, list: string[], emptyLabel: string) => {
    const current = values[key]
    const unavailable = current !== '' && !list.includes(current)
    const labelOf = (o: string) => (unavailable && o === current ? `${o}${NOT_AVAILABLE}` : o)
    const options = unavailable ? [current, ...list] : list
    return (
      <TextField select size="small" label={label} value={current} onChange={(e) => set(key, e.target.value)} fullWidth slotProps={{ select: { displayEmpty: true, renderValue: (v) => (v === '' ? emptyLabel : labelOf(String(v))) } }}>
        <MenuItem value="">{emptyLabel}</MenuItem>
        {options.map((o) => (
          <MenuItem key={o} value={o}>{labelOf(o)}</MenuItem>
        ))}
      </TextField>
    )
  }
  return (
    <>
      <DialogTitle>{preset ? `Edit ${preset.name}` : copyOf ? `Copy ${copyOf.name}` : 'New preset'}</DialogTitle>
      <DialogContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          {serverError && <Alert severity="error" variant="outlined">{serverError}</Alert>}
          {text('name', 'Name', { disabled: preset?.is_default === true })}
          {text('replicas', 'Replicas')}
          {meta && meta.models.length > 0 ? dropdown('model', 'Model', meta.models, 'default') : text('model', 'Model')}
          {meta && meta.efforts.length > 0 ? dropdown('effort', 'Effort', meta.efforts, 'model default') : text('effort', 'Effort')}
          <FormControlLabel control={<Switch checked={values.peers} onChange={(e) => set('peers', e.target.checked)} />} label="Peers" />
          {text('workdir', 'Workdir')}
          {text('pvcSize', 'PVC size')}
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
        <Button variant="outlined" disabled={invalid || busy} onClick={() => void submit()}>{editing ? 'Save' : 'Create'}</Button>
      </DialogActions>
    </>
  )
}

/**
 * @param props see PresetFormProps.
 * @returns the dialog element; its body remounts on every open so the form starts fresh.
 */
export function PresetForm({ open, ...rest }: PresetFormProps) {
  return (
    <Dialog open={open} onClose={rest.onClose} fullWidth maxWidth="sm" aria-label={rest.preset ? 'Edit preset' : rest.copyOf ? 'Copy preset' : 'New preset'}>
      <FormBody {...rest} />
    </Dialog>
  )
}
