// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import App from './App'
import { theme } from './theme'

vi.mock('./api/client', () => ({ listClusters: async () => [], listAgents: async () => [], statusStreamUrl: () => '/api/v1/stream' }))
vi.mock('./api/stream', () => ({ openStream: () => ({ close: () => {} }) }))

afterEach(cleanup)

window.matchMedia = ((query: string) => ({ matches: /min-width/.test(query), media: query, addEventListener: () => {}, removeEventListener: () => {} })) as unknown as typeof window.matchMedia

test('renders the application name', async () => {
  render(
    <ThemeProvider theme={theme}>
      <App />
    </ThemeProvider>,
  )
  expect(await screen.findByText('No clusters yet')).toBeTruthy()
  expect(screen.getAllByText('Agent Orchestrator').length).toBeGreaterThan(0)
})
