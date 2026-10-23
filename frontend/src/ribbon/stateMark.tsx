/** Small square mark of a working, waiting, booting, error or deleting state. */
import Box from '@mui/material/Box'
import { TOKENS } from '../theme'

export type MarkState = 'working' | 'waiting' | 'booting' | 'error' | 'deleting'

const MARK_PX = 8
const COLOUR: Record<MarkState, string> = {
  working: TOKENS.accent,
  waiting: TOKENS.textSecondary,
  booting: TOKENS.warning,
  error: TOKENS.error,
  deleting: TOKENS.textDisabled,
}

/** Maps a wrapper-reported agent state to a mark: a pod that is not ready is booting. */
export function agentMarkState(state: string, ready: boolean): MarkState {
  if (!ready) return 'booting'
  if (state === 'idle') return 'waiting'
  if (state === 'busy' || state === 'working') return 'working'
  if (state === 'error' || state === 'failed') return 'error'
  return 'booting'
}

export function StateMark({ state }: { state: MarkState }) {
  return <Box role="img" aria-label={`state ${state}`} sx={{ width: MARK_PX, height: MARK_PX, flexShrink: 0, bgcolor: COLOUR[state], opacity: state === 'deleting' ? 0.6 : 1 }} />
}
