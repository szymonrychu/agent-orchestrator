// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, test } from 'vitest'
import { theme } from '../theme'
import { useLayout } from './useLayout'

/** Installs a matchMedia that evaluates min-width / max-width queries against `width`. */
function setViewport(width: number) {
  window.matchMedia = ((query: string) => {
    const min = /min-width:\s*([\d.]+)px/.exec(query)
    const max = /max-width:\s*([\d.]+)px/.exec(query)
    const matches = (!min || width >= Number(min[1])) && (!max || width <= Number(max[1]))
    return {
      matches,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }
  }) as typeof window.matchMedia
}

function Probe() {
  return <span data-testid="layout">{useLayout()}</span>
}

function layoutAt(width: number): string {
  setViewport(width)
  render(
    <ThemeProvider theme={theme}>
      <Probe />
    </ThemeProvider>,
  )
  return screen.getByTestId('layout').textContent ?? ''
}

afterEach(cleanup)

describe('useLayout', () => {
  test.each([
    [375, 'phone'],
    [599, 'phone'],
    [600, 'tablet'],
    [1199, 'tablet'],
    [1200, 'desktop'],
    [1920, 'desktop'],
  ])('width %i is %s', (width, expected) => {
    expect(layoutAt(width)).toBe(expected)
  })
})
