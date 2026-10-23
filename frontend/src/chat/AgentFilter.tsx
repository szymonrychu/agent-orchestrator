/** One chip per agent; selected chips narrow the log to those agents, none selected means all. */
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import type { AgentView } from '../api/client'

export interface AgentFilterProps {
  agents: AgentView[]
  selected: readonly string[]
  onToggle: (agentId: string) => void
}

export function AgentFilter({ agents, selected, onToggle }: AgentFilterProps) {
  return (
    <Box role="group" aria-label="agent filter" sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
      {agents.map((a) => {
        const on = selected.includes(a.id)
        return <Chip key={a.id} component="button" clickable aria-label={`agent ${a.ordinal}`} aria-pressed={on} label={`#${a.ordinal}`} variant={on ? 'filled' : 'outlined'} onClick={() => onToggle(a.id)} />
      })}
    </Box>
  )
}
