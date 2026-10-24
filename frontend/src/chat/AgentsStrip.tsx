/** Agents of a cluster in the chat header: state, free-since age and own gauge; clicking one narrows the log to it. */
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import type { AgentView } from '../api/client'
import { ContextGauge } from '../ribbon/ContextGauge'
import { StateMark, agentMarkState } from '../ribbon/stateMark'
import { TOKENS } from '../theme'
import { formatAge, useNow } from './agentAge'

export interface AgentsStripProps {
  /** In ordinal order. */
  agents: AgentView[]
  /** Agent ids the log is narrowed to; none selected means all. */
  selected: readonly string[]
  onToggle: (agentId: string) => void
  autoCompactPct?: number
}

export function AgentsStrip({ agents, selected, onToggle, autoCompactPct }: AgentsStripProps) {
  const now = useNow()
  return (
    <Box role="group" aria-label="agent filter" sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, minWidth: 0 }}>
      {agents.map((a) => {
        const on = selected.includes(a.id)
        const free = a.state === 'idle' && a.free_since ? formatAge(now - Date.parse(a.free_since)) : null
        return (
          <ButtonBase
            key={a.id}
            data-testid={`agent-row-${a.pod_name}`}
            aria-label={`agent ${a.ordinal}`}
            aria-pressed={on}
            onClick={() => onToggle(a.id)}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              px: 1,
              py: 0.5,
              border: `${TOKENS.borderWidth}px solid ${on ? TOKENS.accent : TOKENS.border}`,
              bgcolor: on ? 'action.selected' : 'transparent',
              color: 'text.primary',
            }}
          >
            <StateMark state={agentMarkState(a.state, a.ready)} />
            <Box component="span" sx={{ fontSize: TOKENS.fontSizes.xs, color: 'text.secondary', flexShrink: 0 }}>
              {`#${a.ordinal}`}
            </Box>
            <Box component="span" sx={{ fontSize: TOKENS.fontSizes.sm, whiteSpace: 'nowrap' }}>
              {a.pod_name}
            </Box>
            {free !== null && (
              <Box component="span" sx={{ fontSize: TOKENS.fontSizes.xs, color: 'text.secondary', flexShrink: 0 }}>
                {`free ${free}`}
              </Box>
            )}
            <ContextGauge value={a.context?.used_percentage ?? null} autoCompactPct={autoCompactPct} />
          </ButtonBase>
        )
      })}
    </Box>
  )
}
