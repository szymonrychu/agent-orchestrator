// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, test } from 'vitest'
import { TOKENS, theme } from '../theme'
import { ContextGauge } from './ContextGauge'

afterEach(cleanup)

function setup(props: React.ComponentProps<typeof ContextGauge>) {
  return render(
    <ThemeProvider theme={theme}>
      <ContextGauge {...props} />
    </ThemeProvider>,
  )
}

function hex(rgb: string): string {
  const [r, g, b] = rgb.match(/\d+/g)!.map(Number)
  return `#${[r, g, b].map((n) => n.toString(16).padStart(2, '0')).join('')}`
}

describe('ContextGauge', () => {
  test('renders rounded percentage', () => {
    setup({ value: 42.6 })
    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('43')
    expect(screen.getByText('43%')).toBeTruthy()
    expect(bar.getAttribute('aria-valuemin')).toBe('0')
    expect(bar.getAttribute('aria-valuemax')).toBe('100')
  })

  test('null renders unknown', () => {
    setup({ value: null })
    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-label')).toBe('context unknown')
    expect(bar.hasAttribute('aria-valuenow')).toBe(false)
    expect(screen.getByText('-')).toBeTruthy()
  })

  test('clamps values', () => {
    setup({ value: 140 })
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100')
    cleanup()
    setup({ value: -5 })
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('0')
    expect(screen.getByText('0%')).toBeTruthy()
  })

  test('draws auto compact tick and warns past it', () => {
    setup({ value: 50, autoCompactPct: 80 })
    const tick = screen.getByTestId('context-gauge-tick')
    expect(tick.style.transform).toBe('rotate(288deg)')
    expect(hex(getComputedStyle(screen.getByRole('progressbar')).color)).not.toBe(TOKENS.warning)
    cleanup()
    setup({ value: 80, autoCompactPct: 80 })
    expect(hex(getComputedStyle(screen.getByRole('progressbar')).color)).toBe(TOKENS.warning)
    cleanup()
    setup({ value: 90 })
    expect(screen.queryByTestId('context-gauge-tick')).toBeNull()
  })
})
