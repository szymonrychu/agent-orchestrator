/** One agent of an expanded cluster: ordinal, pod, state mark, own gauge, free-since for idle agents. */
import { useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import type { AgentView } from '../api/client'
import { TOKENS } from '../theme'
import { ContextGauge } from './ContextGauge'
import { StateMark, agentMarkState } from './stateMark'

/** Compact age such as "45s", "2m", "3h", "2d". */
export function formatAge(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

const TICK_MS = 15000

function useNow(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(t)
  }, [])
  return now
}

export function AgentRow({ agent, autoCompactPct }: { agent: AgentView; autoCompactPct: number }) {
  const now = useNow()
  const mark = agentMarkState(agent.state, agent.ready)
  const free = agent.state === 'idle' && agent.free_since ? formatAge(now - Date.parse(agent.free_since)) : null
  return (
    <Box data-testid={`agent-row-${agent.pod_name}`} sx={{ display: 'flex', alignItems: 'center', gap: 1, pl: 3, pr: 1, py: 0.5 }}>
      <StateMark state={mark} />
      <Box component="span" sx={{ fontSize: TOKENS.fontSizes.xs, color: 'text.secondary', flexShrink: 0 }}>
        {`#${agent.ordinal}`}
      </Box>
      <Box component="span" sx={{ fontSize: TOKENS.fontSizes.sm, flexGrow: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {agent.pod_name}
      </Box>
      {free !== null && (
        <Box component="span" sx={{ fontSize: TOKENS.fontSizes.xs, color: 'text.secondary', flexShrink: 0 }}>
          {`free ${free}`}
        </Box>
      )}
      <ContextGauge value={agent.context?.used_percentage ?? null} autoCompactPct={autoCompactPct} />
    </Box>
  )
}
