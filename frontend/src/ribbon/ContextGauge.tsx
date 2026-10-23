/** Circular 0-100% gauge of how much of the model context window a session has used. */
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { TOKENS } from '../theme'

export interface ContextGaugeProps {
  /** Used percentage of the full model window; null when unknown. */
  value: number | null
  /** Percentage at which the wrapper auto-compacts; draws a tick and warns at or above it. */
  autoCompactPct?: number
  size?: 'small' | 'medium'
  /** Accessible name; defaults to "context N%" or "context unknown". */
  label?: string
}

const SIZES = { small: 28, medium: 40 } as const
const THICKNESS = 4
/** MUI CircularProgress draws in a 44-unit box; the ring spans radius 18 to 22 from its centre. */
const VIEW = 44

const clamp = (n: number): number => Math.min(100, Math.max(0, n))

export function ContextGauge({ value, autoCompactPct, size = 'small', label }: ContextGaugeProps) {
  const known = value !== null && Number.isFinite(value)
  const pct = known ? Math.round(clamp(value)) : 0
  const px = SIZES[size]
  const tick = autoCompactPct === undefined ? null : clamp(autoCompactPct)
  const warn = known && tick !== null && pct >= tick
  const name = label ?? (known ? `context ${pct}%` : 'context unknown')
  return (
    <Box sx={{ position: 'relative', display: 'inline-flex', width: px, height: px, flexShrink: 0 }}>
      <CircularProgress variant="determinate" value={100} size={px} thickness={THICKNESS} aria-hidden sx={{ position: 'absolute', color: TOKENS.border }} />
      <CircularProgress
        variant="determinate"
        value={pct}
        size={px}
        thickness={THICKNESS}
        aria-label={name}
        aria-valuenow={known ? pct : undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        sx={{ position: 'absolute', color: warn ? TOKENS.warning : TOKENS.accent }}
      />
      {tick !== null && (
        <svg viewBox={`0 0 ${VIEW} ${VIEW}`} width={px} height={px} aria-hidden style={{ position: 'absolute', inset: 0 }}>
          <line
            data-testid="context-gauge-tick"
            x1={VIEW / 2}
            x2={VIEW / 2}
            y1={0}
            y2={THICKNESS}
            transform={`rotate(${tick * 3.6} ${VIEW / 2} ${VIEW / 2})`}
            stroke={TOKENS.textPrimary}
            strokeWidth={1.5}
          />
        </svg>
      )}
      <Box
        sx={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: `${(size === 'small' ? 8 : TOKENS.fontSizes.xs) / 16}rem`,
          fontWeight: TOKENS.fontWeights.medium,
          color: TOKENS.textSecondary,
        }}
      >
        {known ? `${pct}%` : '-'}
      </Box>
    </Box>
  )
}
