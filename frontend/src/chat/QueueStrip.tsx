/** Queued, verifying and failed messages of a cluster, with Cancel and Retry; hidden when empty. */
import { useCallback, useEffect, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { cancelMessage, listMessages, retryMessage, statusStreamUrl } from '../api/client'
import type { MessageView } from '../api/client'
import { openStream } from '../api/stream'
import { TOKENS } from '../theme'

export const QUEUE_STATUSES = 'queued,verifying,failed'

const errorText = (e: unknown): string => (e instanceof Error ? e.message : String(e))
const isAmbiguous = (m: MessageView): boolean => /ambiguous/i.test(m.error ?? '')

function detail(m: MessageView): string | null {
  if (m.status === 'verifying') return 'checking delivery'
  if (m.status === 'queued' && m.next_attempt_at) return `attempt ${m.attempts} of ${m.max_attempts}, next attempt ${new Date(m.next_attempt_at).toLocaleString()}`
  return null
}

export function QueueStrip({ clusterId }: { clusterId: string }) {
  const [shown, setShown] = useState<{ clusterId: string; messages: MessageView[] }>({ clusterId, messages: [] })
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<number | null>(null)
  const latest = useRef(0)

  const refresh = useCallback(() => {
    const mine = ++latest.current
    listMessages(clusterId, { status: QUEUE_STATUSES })
      .then((messages) => {
        if (latest.current === mine) setShown({ clusterId, messages })
      })
      .catch((e: unknown) => {
        if (latest.current === mine) setError(errorText(e))
      })
  }, [clusterId])

  useEffect(() => {
    const token = latest
    refresh()
    const stream = openStream(statusStreamUrl(), {
      eventNames: ['message_status'],
      onEvent: (data) => {
        if ((data as { cluster_id?: string } | null)?.cluster_id === clusterId) refresh()
      },
    })
    return () => {
      token.current++
      stream.close()
    }
  }, [clusterId, refresh])

  const act = (call: () => Promise<unknown>) => {
    setError(null)
    setConfirming(null)
    call().then(refresh, (e: unknown) => setError(errorText(e)))
  }

  const messages = shown.clusterId === clusterId ? shown.messages : []
  if (messages.length === 0 && !error) return null
  return (
    <Box sx={{ py: 1, borderTop: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
      {error ? (
        <Alert severity="error" variant="outlined" sx={{ mb: 1 }}>
          {error}
        </Alert>
      ) : null}
      {messages.map((m) => (
        <Box key={m.id} sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.5 }}>
          <Typography variant="caption" sx={{ color: 'text.secondary', minWidth: 64 }}>
            {m.status}
          </Typography>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography noWrap>{m.text}</Typography>
            {detail(m) ? (
              <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                {detail(m)}
              </Typography>
            ) : null}
            {m.status === 'failed' && m.error ? (
              <Typography variant="caption" sx={{ color: 'error.main', display: 'block' }}>
                {m.error}
              </Typography>
            ) : null}
            {confirming === m.id ? (
              <Typography variant="caption" sx={{ color: 'warning.main', display: 'block' }}>
                This turn may already have run; retrying could send it twice.
              </Typography>
            ) : null}
          </Box>
          {m.status === 'queued' ? (
            <Button size="small" onClick={() => act(() => cancelMessage(String(m.id)))}>
              Cancel
            </Button>
          ) : null}
          {m.status === 'failed' ? (
            confirming === m.id ? (
              <Button size="small" color="warning" onClick={() => act(() => retryMessage(String(m.id)))}>
                Retry anyway
              </Button>
            ) : (
              <Button size="small" onClick={() => (isAmbiguous(m) ? setConfirming(m.id) : act(() => retryMessage(String(m.id))))}>
                Retry
              </Button>
            )
          ) : null}
        </Box>
      ))}
    </Box>
  )
}
