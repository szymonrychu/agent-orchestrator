// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import type { AgentView, ClusterView } from '../api/client'
import type { StreamOptions } from '../api/stream'
import App from '../App'
import { theme } from '../theme'

const api = vi.hoisted(() => ({ listClusters: vi.fn(), listAgents: vi.fn() }))
const stream = vi.hoisted(() => ({ opts: null as null | StreamOptions, close: vi.fn() }))

vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), ...api, statusStreamUrl: () => '/api/v1/stream' }))
vi.mock('../api/stream', () => ({
  openStream: (_url: string, opts: StreamOptions) => {
    stream.opts = opts
    return { close: stream.close }
  },
}))

function setViewport(width: number) {
  window.matchMedia = ((query: string) => {
    const min = /min-width:\s*([\d.]+)px/.exec(query)
    const max = /max-width:\s*([\d.]+)px/.exec(query)
    const matches = (!min || width >= Number(min[1])) && (!max || width <= Number(max[1]))
    return { matches, media: query, onchange: null, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }
  }) as typeof window.matchMedia
}

const summary = (o: Partial<ClusterView['summary']> = {}): ClusterView['summary'] => ({
  auto_compact_pct: 80, context_pct_max: 40, failed: 0, queued: 0, ready: 2, replicas: 3, state: 'working', ...o,
})
const cluster = (id: string, slug: string, s: Partial<ClusterView['summary']> = {}): ClusterView =>
  ({ created_at: '2026-01-01T00:00:00Z', desired: 'active', generation: 1, id, observed_generation: 1, slug, spec: {} as ClusterView['spec'], status: {}, summary: summary(s) }) as ClusterView
const agent = (id: string, ordinal: number, o: Partial<AgentView> = {}): AgentView => ({
  context: { used_percentage: 25, used_tokens: 1, window_tokens: 4 }, current_turn_id: null, free_since: null, gone: false, id, ordinal, peers: 0, pod_name: `alpha-${ordinal}`, ready: true, state: 'busy', ...o,
})

function emit(type: string, data: unknown) {
  act(() => stream.opts!.onEvent(data, { type, id: '1' }))
}

function mount() {
  return render(
    <ThemeProvider theme={theme}>
      <App />
    </ThemeProvider>,
  )
}

beforeEach(() => {
  setViewport(1400)
  stream.opts = null
  api.listClusters.mockResolvedValue([cluster('c1', 'alpha'), cluster('c2', 'beta', { state: 'waiting', ready: 1, replicas: 1, context_pct_max: 10 })])
  api.listAgents.mockImplementation(async (id: string) =>
    id === 'c1' ? [agent('a0', 0), agent('a1', 1, { state: 'idle', free_since: new Date(Date.now() - 120000).toISOString(), context: { used_percentage: 70, used_tokens: 1, window_tokens: 2 } })] : [],
  )
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Ribbon', () => {
  test('renders clusters with state count and gauge', async () => {
    mount()
    const row = await screen.findByTestId('cluster-row-alpha')
    expect(within(row).getByText('alpha')).toBeTruthy()
    expect(within(row).getByText('2/3')).toBeTruthy()
    expect(within(row).getByLabelText('state working')).toBeTruthy()
    expect(within(row).getByRole('progressbar', { name: 'context 40%' })).toBeTruthy()
    expect(within(row).getByTestId('context-gauge-tick')).toBeTruthy()
    const other = screen.getByTestId('cluster-row-beta')
    expect(within(other).getByLabelText('state waiting')).toBeTruthy()
    expect(within(other).getByText('1/1')).toBeTruthy()
  })

  test('shows an empty state without clusters', async () => {
    api.listClusters.mockResolvedValue([])
    mount()
    expect(await screen.findByText('No clusters yet')).toBeTruthy()
  })

  test('expands to agents with own gauges', async () => {
    mount()
    const row = await screen.findByTestId('cluster-row-alpha')
    expect(screen.queryByTestId('agent-row-alpha-0')).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'expand alpha' }))
    const a0 = await screen.findByTestId('agent-row-alpha-0')
    const a1 = screen.getByTestId('agent-row-alpha-1')
    expect(within(a0).getByRole('progressbar', { name: 'context 25%' })).toBeTruthy()
    expect(within(a1).getByRole('progressbar', { name: 'context 70%' })).toBeTruthy()
    expect(within(a0).getByText('alpha-0')).toBeTruthy()
    expect(within(a1).getByLabelText('state waiting')).toBeTruthy()
    expect(within(a1).getByText(/free 2m/)).toBeTruthy()
    expect(within(a0).queryByText(/free/)).toBeNull()
  })

  test('applies stream deltas', async () => {
    mount()
    const row = await screen.findByTestId('cluster-row-alpha')
    fireEvent.click(within(row).getByRole('button', { name: 'expand alpha' }))
    await screen.findByTestId('agent-row-alpha-0')
    const head = screen.getByTestId('cluster-head-alpha')
    emit('cluster_status', { cluster: cluster('c1', 'alpha', { state: 'waiting', context_pct_max: 77, ready: 3 }) })
    expect(within(head).getByLabelText('state waiting')).toBeTruthy()
    expect(within(head).getByRole('progressbar', { name: 'context 77%' })).toBeTruthy()
    expect(within(head).getByText('3/3')).toBeTruthy()
    emit('agent_status', { cluster_id: 'c1', agent: agent('a0', 0, { state: 'idle', context: { used_percentage: 90, used_tokens: 1, window_tokens: 2 } }) })
    expect(within(screen.getByTestId('agent-row-alpha-0')).getByRole('progressbar', { name: 'context 90%' })).toBeTruthy()
    emit('cluster_status', { cluster: cluster('c3', 'gamma', { state: 'booting', ready: 0 }) })
    expect(screen.getByTestId('cluster-row-gamma')).toBeTruthy()
    emit('message_status', { cluster_id: 'c1', queued: 1, failed: 0, messages: [] })
    emit('cluster_removed', { cluster_id: 'c2' })
    expect(screen.queryByTestId('cluster-row-beta')).toBeNull()
    emit('snapshot', { clusters: [{ cluster: cluster('c9', 'omega'), agents: [] }] })
    expect(screen.queryByTestId('cluster-row-alpha')).toBeNull()
    expect(screen.getByTestId('cluster-row-omega')).toBeTruthy()
  })

  test('selecting a row selects the cluster', async () => {
    mount()
    expect(await screen.findByText('Select a cluster')).toBeTruthy()
    fireEvent.click(await screen.findByRole('button', { name: 'select alpha' }))
    const main = screen.getByRole('main')
    expect(within(main).getByText('alpha')).toBeTruthy()
    expect(screen.queryByText('Select a cluster')).toBeNull()
    expect(screen.getByTestId('cluster-row-alpha').getAttribute('aria-current')).toBe('true')
  })

  test('phone layout uses temporary drawer', async () => {
    setViewport(400)
    mount()
    expect(screen.queryByTestId('ribbon-aside')).toBeNull()
    expect(screen.queryByTestId('cluster-row-alpha')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'open clusters' }))
    const drawer = await screen.findByTestId('ribbon-drawer')
    expect(drawer.className).toContain('MuiDrawer-modal')
    fireEvent.click(await within(drawer).findByRole('button', { name: 'select alpha' }))
    await waitFor(() => expect(within(screen.getByRole('main')).getByText('alpha')).toBeTruthy())
  })

  test('desktop layout uses a permanent aside', async () => {
    mount()
    expect(await screen.findByTestId('ribbon-aside')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'open clusters' })).toBeNull()
  })
})
