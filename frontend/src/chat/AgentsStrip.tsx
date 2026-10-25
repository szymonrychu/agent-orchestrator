/** Agents of a cluster in the chat header: state, free-since age and own gauge; clicking one narrows the log to it. */
import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import type { AgentView } from '../api/client'
import { makeLeader } from '../api/client'
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
  /** Set for hive clusters only: enables the leader badge and the Make leader action. Alone clusters omit it. */
  hive?: { clusterId: string; leaderEpoch: number | null }
}

export function AgentsStrip({ agents, selected, onToggle, autoCompactPct, hive }: AgentsStripProps) {
  const now = useNow()
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<string | null>(null)
  const onMakeLeader = async (clusterId: string, agentId: string) => {
    setError(null)
    setPending(agentId)
    try {
      await makeLeader(clusterId, agentId)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(null)
    }
  }
  return (
    <Box role="group" aria-label="agent filter" sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, minWidth: 0 }}>
      {error !== null && (
        <Box role="alert" sx={{ width: '100%', fontSize: TOKENS.fontSizes.xs, color: 'error.main' }}>
          {error}
        </Box>
      )}
      {agents.map((a) => {
        const on = selected.includes(a.id)
        const free = a.state === 'idle' && a.free_since ? formatAge(now - Date.parse(a.free_since)) : null
        return (
          <Box key={a.id} data-testid={`agent-row-${a.pod_name}`} sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
          <ButtonBase
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
            {hive && a.leader && (
              <Box
                component="span"
                title={hive.leaderEpoch === null ? 'leader' : `leader epoch ${hive.leaderEpoch}`}
                sx={{ fontSize: TOKENS.fontSizes.xs, color: TOKENS.accent, border: `${TOKENS.borderWidth}px solid ${TOKENS.accent}`, px: 0.5, flexShrink: 0 }}
              >
                leader
              </Box>
            )}
            <ContextGauge value={a.context_pct ?? a.context?.used_percentage ?? null} autoCompactPct={autoCompactPct} />
          </ButtonBase>
          {hive && !a.leader && (
            <Button size="small" disabled={pending === a.id} onClick={() => void onMakeLeader(hive.clusterId, a.id)}>
              Make leader
            </Button>
          )}
          </Box>
        )
      })}
    </Box>
  )
}
