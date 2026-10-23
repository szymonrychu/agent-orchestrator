/** Message input of one cluster: Enter sends, Shift+Enter is a newline, target is any free agent or one ordinal. */
import { useState } from 'react'
import type { KeyboardEvent } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MenuItem from '@mui/material/MenuItem'
import Select from '@mui/material/Select'
import TextField from '@mui/material/TextField'
import { postMessage } from '../api/client'
import type { AgentView } from '../api/client'
import { TOKENS } from '../theme'

export interface ComposerProps {
  clusterId: string
  agents: AgentView[]
  deleting: boolean
}

const ANY = ''

export function Composer({ clusterId, agents, deleting }: ComposerProps) {
  const [text, setText] = useState('')
  const [target, setTarget] = useState(ANY)
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const canSend = text.trim() !== '' && !deleting && !sending
  // A chosen agent that left the list falls back to any free agent.
  const chosen = agents.some((a) => a.id === target) ? target : ANY

  const send = () => {
    if (!canSend) return
    setSending(true)
    setError(null)
    postMessage(clusterId, chosen === ANY ? { text } : { text, target_agent_id: chosen })
      .then(() => setText(''))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSending(false))
  }
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key !== 'Enter' || e.shiftKey) return
    e.preventDefault()
    send()
  }

  return (
    <Box sx={{ pt: 1, borderTop: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
      {error ? (
        <Alert severity="error" variant="outlined" sx={{ mb: 1 }}>
          {error}
        </Alert>
      ) : null}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
        <TextField
          multiline
          maxRows={8}
          fullWidth
          size="small"
          placeholder="Message the cluster"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          slotProps={{ htmlInput: { 'aria-label': 'Message' } }}
        />
        <Select size="small" value={chosen} onChange={(e) => setTarget(e.target.value)} inputProps={{ 'aria-label': 'Target agent' }} sx={{ minWidth: 160 }}>
          <MenuItem value={ANY}>Any free agent</MenuItem>
          {agents.map((a) => (
            <MenuItem key={a.id} value={a.id}>
              Agent {a.ordinal}
            </MenuItem>
          ))}
        </Select>
        <Button variant="contained" onClick={send} disabled={!canSend}>
          Send
        </Button>
      </Box>
    </Box>
  )
}
