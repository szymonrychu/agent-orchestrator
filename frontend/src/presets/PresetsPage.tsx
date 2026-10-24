/** Settings > Presets: the stored ClusterSpec templates, with create, edit, copy and delete. */
import { useEffect, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import List from '@mui/material/List'
import ListItem from '@mui/material/ListItem'
import ListItemText from '@mui/material/ListItemText'
import Typography from '@mui/material/Typography'
import { deletePreset, listPresets } from '../api/client'
import type { PresetView } from '../api/client'
import { ConfirmDialog } from '../layout/ConfirmDialog'
import { TOKENS } from '../theme'
import { PresetForm } from './PresetForm'

export interface PresetsPageProps {
  onBack: () => void
}

/** The open editor: a new preset, an edit of one, or a copy of one. */
type Editor = { mode: 'new' } | { mode: 'edit'; preset: PresetView } | { mode: 'copy'; preset: PresetView }

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

export function PresetsPage({ onBack }: PresetsPageProps) {
  const [presets, setPresets] = useState<PresetView[]>([])
  const [editor, setEditor] = useState<Editor | null>(null)
  const [deleting, setDeleting] = useState<PresetView | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Bumped after every save or delete; the effect below refetches the list.
  const [version, setVersion] = useState(0)
  const reload = () => setVersion((v) => v + 1)

  useEffect(() => {
    let cancelled = false
    listPresets()
      .then((list) => {
        if (cancelled) return
        // The default preset first, the rest in the server's order.
        setPresets([...list.filter((p) => p.is_default), ...list.filter((p) => !p.is_default)])
        setError(null)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(message(e))
      })
    return () => {
      cancelled = true
    }
  }, [version])

  async function confirmDelete(preset: PresetView) {
    setDeleting(null)
    try {
      await deletePreset(preset.id)
    } catch (e) {
      setError(message(e))
      return
    }
    reload()
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, minHeight: 0, overflow: 'auto' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Button onClick={onBack}>Back</Button>
        <Typography component="h1" sx={{ flexGrow: 1, fontSize: TOKENS.fontSizes.lg }}>Presets</Typography>
        <Button variant="outlined" onClick={() => setEditor({ mode: 'new' })}>New preset</Button>
      </Box>
      {error && <Alert severity="error" variant="outlined">{error}</Alert>}
      <List disablePadding>
        {presets.map((p) => (
          <ListItem
            key={p.id}
            divider
            secondaryAction={
              <Box sx={{ display: 'flex', gap: 1 }}>
                <Button size="small" aria-label={`Edit ${p.name}`} onClick={() => setEditor({ mode: 'edit', preset: p })}>Edit</Button>
                <Button size="small" aria-label={`Copy ${p.name}`} onClick={() => setEditor({ mode: 'copy', preset: p })}>Copy</Button>
                {!p.is_default && <Button size="small" color="error" aria-label={`Delete ${p.name}`} onClick={() => setDeleting(p)}>Delete</Button>}
              </Box>
            }
          >
            <ListItemText primary={p.name} />
            {p.is_default && <Chip size="small" label="Default" sx={{ ml: 1 }} />}
          </ListItem>
        ))}
      </List>
      {editor && (
        <PresetForm
          open
          preset={editor.mode === 'edit' ? editor.preset : undefined}
          copyOf={editor.mode === 'copy' ? editor.preset : undefined}
          onClose={() => setEditor(null)}
          onSaved={() => {
            setEditor(null)
            reload()
          }}
        />
      )}
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete preset ${deleting?.name ?? ''}?`}
        body="Clusters already created from it keep their own copy of the spec."
        confirmLabel="Delete"
        onConfirm={() => deleting && void confirmDelete(deleting)}
        onCancel={() => setDeleting(null)}
      />
    </Box>
  )
}
