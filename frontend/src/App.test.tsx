// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import type { AgentView, ClusterView, Meta } from './api/client'
import type { StreamOptions } from './api/stream'
import App from './App'
import { theme } from './theme'

const api = vi.hoisted(() => ({ listClusters: vi.fn(), listAgents: vi.fn(), getMeta: vi.fn(), createCluster: vi.fn(), deleteCluster: vi.fn() }))
const stream = vi.hoisted(() => ({ opts: null as null | StreamOptions, close: vi.fn() }))

vi.mock('./api/client', async (orig) => ({ ...(await orig<typeof import('./api/client')>()), ...api, statusStreamUrl: () => '/api/v1/stream' }))
vi.mock('./api/stream', () => ({
  openStream: (_url: string, opts: StreamOptions) => {
    // The chat view opens its own stream; keep the cluster status stream.
    if (opts.eventNames?.includes('cluster_status')) stream.opts = opts
    return { close: stream.close }
  },
}))

/** jsdom has no layout: the workspace's panel library needs observers and a box. */
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

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
const agent = (id: string, ordinal: number): AgentView => ({
  context: { used_percentage: 25, used_tokens: 1, window_tokens: 4 }, current_turn_id: null, free_since: null, gone: false, id, ordinal, peers: 0, pod_name: `alpha-${ordinal}`, ready: true, state: 'busy',
})
const meta = (): Meta => ({
  agent_image: 'img', agent_images: ['img'], caps: { max_clusters: 8, max_pvc_size: '100Gi', max_replicas: 16 },
  defaults: {
    any_mcp_config: { plugins: ['x'] }, auto_compact_pct: 80, discard_hook: 'none', effort: 'high', max_attempts: 5, model: 'opus', pvc_size: '10Gi', replicas: 1,
    resources: { limits: { cpu: '4', memory: '8Gi' }, requests: { cpu: '500m', memory: '1Gi' } }, storage_class: 'rook-ceph-rwx', workdir: 'workdir',
  },
  discard_hook: 'none', models: ['opus', 'sonnet', 'haiku'], efforts: ['low', 'medium', 'high', 'max'],
}) as Meta

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

const tabs = () => screen.getAllByRole('tab')
const selectedTab = () => tabs().find((t) => t.getAttribute('aria-selected') === 'true')

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', NoopObserver)
  localStorage.clear()
  setViewport(1400)
  stream.opts = null
  api.listClusters.mockResolvedValue([cluster('c2', 'beta', { state: 'waiting', ready: 1, replicas: 1, context_pct_max: 10 }), cluster('c1', 'alpha')])
  api.listAgents.mockImplementation(async (id: string) => (id === 'c1' ? [agent('a0', 0), agent('a1', 1)] : []))
  api.getMeta.mockResolvedValue(meta())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('App', () => {
  test('top_bar_replaces_ribbon', async () => {
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    expect(tabs().map((t) => t.textContent)).toEqual(['alpha40%', 'beta10%'])
    expect(within(tabs()[0]).getByRole('img', { name: 'state working' })).toBeTruthy()
    expect(within(tabs()[1]).getByRole('img', { name: 'state waiting' })).toBeTruthy()
    expect(within(tabs()[0]).getByTestId('context-gauge-tick')).toBeTruthy()
    const main = screen.getByRole('main')
    expect(within(main).getByRole('heading', { name: 'alpha' })).toBeTruthy()
    expect(screen.queryByRole('complementary')).toBeNull()
    expect(screen.queryByTestId('ribbon-aside')).toBeNull()
    expect(screen.queryByTestId('ribbon-drawer')).toBeNull()
    expect(screen.queryByTestId('cluster-row-alpha')).toBeNull()
  })

  test('only_active_cluster_workspace_mounted', async () => {
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    const workspaces = () => screen.queryAllByTestId('workspace').map((w) => w.getAttribute('data-cluster'))
    expect(workspaces()).toEqual(['c1'])
    const main = screen.getByRole('main')
    expect(within(main).getByRole('button', { name: 'Minimize Files' })).toBeTruthy()
    expect(within(main).getByRole('button', { name: 'Minimize Chat' })).toBeTruthy()
    expect(within(main).getByRole('button', { name: 'Minimize Terminal' })).toBeTruthy()
    expect(within(main).getByRole('heading', { name: 'alpha' })).toBeTruthy()
    fireEvent.click(tabs()[1])
    expect(workspaces()).toEqual(['c2'])
    expect(within(main).queryByRole('heading', { name: 'alpha' })).toBeNull()
    expect(within(main).getByRole('heading', { name: 'beta' })).toBeTruthy()
    // The layout of the tab left behind is remembered for when it is shown again.
    fireEvent.click(within(main).getByRole('button', { name: 'Minimize Files' }))
    fireEvent.click(tabs()[0])
    expect(workspaces()).toEqual(['c1'])
    expect(within(main).getByRole('button', { name: 'Minimize Files' })).toBeTruthy()
    fireEvent.click(tabs()[1])
    expect(within(main).getByRole('button', { name: 'Restore Files' })).toBeTruthy()
  })

  test('top_bar_on_phone_layout', async () => {
    setViewport(400)
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'New cluster' })).toBeTruthy()
    expect(screen.queryByRole('complementary')).toBeNull()
  })

  test('first_tab_selected_by_default', async () => {
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    expect(selectedTab()?.textContent).toContain('alpha')
    fireEvent.click(tabs()[1])
    expect(selectedTab()?.textContent).toContain('beta')
    expect(within(screen.getByRole('main')).getByRole('heading', { name: 'beta' })).toBeTruthy()
    emit('cluster_removed', { cluster_id: 'c2' })
    await waitFor(() => expect(tabs()).toHaveLength(1))
    expect(selectedTab()?.textContent).toContain('alpha')
    expect(within(screen.getByRole('main')).getByRole('heading', { name: 'alpha' })).toBeTruthy()
    emit('cluster_removed', { cluster_id: 'c1' })
    expect(await screen.findByText('No clusters yet')).toBeTruthy()
    emit('cluster_status', { cluster: cluster('c0', 'aaa') })
    await waitFor(() => expect(selectedTab()?.textContent).toContain('aaa'))
  })

  test('empty_state_keeps_new_cluster_button', async () => {
    api.listClusters.mockResolvedValue([])
    mount()
    expect(await screen.findByText('No clusters yet')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'New cluster' })).toBeTruthy()
  })

  test('list_error_is_shown', async () => {
    api.listClusters.mockRejectedValue(new Error('boom'))
    mount()
    expect((await screen.findByRole('alert')).textContent).toContain('boom')
    expect(screen.queryByText('No clusters yet')).toBeNull()
  })

  test('applies_stream_deltas_to_tabs', async () => {
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    emit('cluster_status', { cluster: cluster('c1', 'alpha', { state: 'waiting', context_pct_max: 77 }) })
    expect(within(tabs()[0]).getByRole('img', { name: 'state waiting' })).toBeTruthy()
    expect(within(tabs()[0]).getByLabelText('context 77%')).toBeTruthy()
    emit('cluster_status', { cluster: cluster('c3', 'gamma', { state: 'booting', ready: 0 }) })
    expect(tabs().map((t) => t.textContent?.replace(/\d+%$/, ''))).toEqual(['alpha', 'beta', 'gamma'])
    emit('snapshot', { clusters: [{ cluster: cluster('c9', 'omega'), agents: [] }] })
    expect(tabs().map((t) => t.textContent?.replace(/\d+%$/, ''))).toEqual(['omega'])
  })

  test('hamburger_opens_sessions_menu_with_empty_settings', async () => {
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const list = screen.getByRole('list', { name: 'Sessions' })
    expect(within(list).getAllByRole('button').map((b) => b.textContent)).toEqual(['alpha', 'beta'])
    expect(screen.getByText('No settings yet')).toBeTruthy()
    fireEvent.click(within(list).getAllByRole('button')[1])
    await waitFor(() => expect(selectedTab()?.textContent).toContain('beta'))
  })

  test('create_and_discard_from_top_bar', { timeout: 15000 }, async () => {
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    fireEvent.click(screen.getByRole('button', { name: 'New cluster' }))
    expect(await screen.findByLabelText('Slug')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByLabelText('Slug')).toBeNull())

    const created = cluster('c5', 'zeta')
    api.createCluster.mockResolvedValue(created)
    fireEvent.click(screen.getByRole('button', { name: 'New cluster' }))
    const slug = (await screen.findByLabelText('Slug')) as HTMLInputElement
    await waitFor(() => expect((screen.getByLabelText('Max attempts') as HTMLInputElement).value).toBe('5'))
    fireEvent.change(slug, { target: { value: 'zeta' } })
    fireEvent.click(screen.getByRole('button', { name: /^(Create|Save)/ }))
    await waitFor(() => expect(api.createCluster).toHaveBeenCalled())
    emit('cluster_status', { cluster: created })
    await waitFor(() => expect(selectedTab()?.textContent).toContain('zeta'))
    await waitFor(() => expect(screen.queryByLabelText('Slug')).toBeNull())

    fireEvent.click(screen.getAllByRole('button', { name: 'Cluster actions' })[0])
    fireEvent.click(screen.getByRole('menuitem', { name: 'Discard' }))
    expect(await screen.findByLabelText('Type the slug to confirm')).toBeTruthy()
  })

  test('no_edit_entry_point', async () => {
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    fireEvent.click(screen.getAllByRole('button', { name: 'Cluster actions' })[0])
    expect(screen.getAllByRole('menuitem').map((m) => m.textContent)).toEqual(['Discard'])
    expect(screen.queryByRole('menuitem', { name: 'Edit' })).toBeNull()
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
  })
})

describe('ribbon retirement', () => {
  test('ribbon_files_retired', () => {
    // Filesystem check via Vite's glob (keys only, nothing is imported), independent of git state.
    const onDisk = Object.keys(import.meta.glob(['./ribbon/*', './layout/*']))
    for (const gone of ['./ribbon/Ribbon.tsx', './ribbon/ClusterRow.tsx', './ribbon/Ribbon.test.tsx', './layout/Shell.tsx']) {
      expect(onDisk).not.toContain(gone)
    }
    expect(onDisk).toContain('./ribbon/useClusters.ts')
  })
})
