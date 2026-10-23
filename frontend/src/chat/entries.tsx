/** One chat row per item kind, each tagged with its agent ordinal. */
import { useState } from 'react'
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Typography from '@mui/material/Typography'
import { MONO_FONT, TOKENS } from '../theme'
import type { ChatItem } from './chatModel'

const TAG_PX = 36

const ordinalLabel = (n: number | undefined): string => (n === undefined ? 'agent ?' : `agent ${n}`)

function Row({ item, label, children }: { item: ChatItem; label?: string; children: ReactNode }) {
  return (
    <Box aria-label={`${ordinalLabel(item.agentOrdinal)} row`} role="listitem" sx={{ display: 'flex', gap: 1, px: 2, py: 0.5, alignItems: 'flex-start' }}>
      <Typography component="span" variant="caption" sx={{ width: TAG_PX, flexShrink: 0, fontFamily: MONO_FONT, color: 'text.secondary' }}>
        {item.agentOrdinal === undefined ? '#?' : `#${item.agentOrdinal}`}
      </Typography>
      <Box sx={{ minWidth: 0, flexGrow: 1 }}>
        {label ? (
          <Typography component="span" variant="caption" sx={{ color: 'text.secondary', mr: 1 }}>
            {label}
          </Typography>
        ) : null}
        {children}
      </Box>
    </Box>
  )
}

const bodyText = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const
const pre = { m: 0, fontFamily: MONO_FONT, fontSize: `${TOKENS.fontSizes.sm / 16}rem`, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', color: 'text.secondary', maxHeight: 320, overflow: 'auto' } as const

function Expandable({ summary, children }: { summary: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <Box>
      <ButtonBase aria-expanded={open} onClick={() => setOpen((o) => !o)} sx={{ gap: 1, textAlign: 'left', color: 'text.secondary', fontSize: `${TOKENS.fontSizes.sm / 16}rem` }}>
        {summary}
      </ButtonBase>
      {open ? <Box sx={{ mt: 0.5 }}>{children}</Box> : null}
    </Box>
  )
}

const json = (v: unknown): string => {
  try {
    return JSON.stringify(v ?? {}, null, 2)
  } catch {
    return String(v)
  }
}

const STATUS_COLOUR = { running: 'text.secondary', ok: 'text.primary', error: 'error.main' } as const

function tokens(usage: Record<string, unknown> | undefined): string {
  if (!usage) return ''
  const n = (k: string) => (typeof usage[k] === 'number' ? (usage[k] as number) : undefined)
  const inT = n('input_tokens')
  const outT = n('output_tokens')
  const parts = [inT === undefined ? '' : `${inT} in`, outT === undefined ? '' : `${outT} out`].filter(Boolean)
  return parts.length ? ` (${parts.join(', ')})` : ''
}

export function ChatEntry({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case 'user_message':
      return (
        <Row item={item} label={item.origin === 'user' ? 'you' : item.origin}>
          <Typography component="span" sx={{ ...bodyText, color: 'text.primary', fontWeight: TOKENS.fontWeights.medium }}>
            {item.text}
          </Typography>
        </Row>
      )
    case 'peer':
      return (
        <Row item={item} label={`peer from ${item.from ?? 'unknown'}`}>
          <Typography component="span" sx={bodyText}>
            {item.text}
          </Typography>
        </Row>
      )
    case 'assistant':
      return (
        <Row item={item}>
          <Typography component="span" sx={bodyText}>
            {item.text}
          </Typography>
        </Row>
      )
    case 'thinking':
      return (
        <Row item={item}>
          <Expandable summary="thinking">
            <Typography component="div" sx={{ ...bodyText, color: 'text.secondary' }}>
              {item.text}
            </Typography>
          </Expandable>
        </Row>
      )
    case 'tool':
      return (
        <Row item={item}>
          <Expandable
            summary={
              <>
                <span>{item.name}</span>
                <Box component="span" sx={{ color: STATUS_COLOUR[item.status] }}>
                  {item.status}
                </Box>
              </>
            }
          >
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              Input
            </Typography>
            <Box component="pre" sx={pre}>
              {json(item.input)}
            </Box>
            {item.output !== undefined ? (
              <>
                <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                  Output
                </Typography>
                <Box component="pre" sx={pre}>
                  {item.output}
                </Box>
              </>
            ) : null}
          </Expandable>
        </Row>
      )
    case 'denied':
      return (
        <Row item={item} label="denied">
          <Typography component="span" sx={{ color: 'warning.main' }}>
            {item.name}
            {item.reason ? `: ${item.reason}` : ''}
          </Typography>
        </Row>
      )
    case 'turn_end':
      return (
        <Row item={item}>
          <Typography component="span" variant="caption" sx={{ color: 'text.secondary' }}>
            {`turn ended${tokens(item.usage)}`}
          </Typography>
        </Row>
      )
    case 'error':
      return (
        <Row item={item} label="error">
          <Typography component="span" sx={{ ...bodyText, color: 'error.main' }}>
            {item.message}
          </Typography>
        </Row>
      )
    case 'gap':
      return (
        <Row item={item}>
          <Typography component="span" variant="caption" sx={{ color: 'text.secondary' }}>
            {item.fromSeq !== undefined && item.toSeq !== undefined ? `gap: events ${item.fromSeq} to ${item.toSeq} are missing` : 'gap: some events are missing'}
          </Typography>
        </Row>
      )
  }
}
