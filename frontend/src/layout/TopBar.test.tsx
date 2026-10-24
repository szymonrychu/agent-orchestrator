// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import type { ClusterView } from '../api/client'
import type { ClusterEntry } from '../ribbon/useClusters'
import { theme } from '../theme'
import { TopBar } from './TopBar'

afterEach(cleanup)

const cluster = (id: string, slug: string, state: ClusterView['summary']['state'], pct: number | null): ClusterEntry => ({
  cluster: { created_at: '2026-01-01T00:00:00Z', desired: 'active', generation: 1, id, observed_generation: 1, slug, spec: {} as ClusterView['spec'], status: {}, summary: { auto_compact_pct: 80, context_pct_max: pct, failed: 0, queued: 0, ready: 1, replicas: 1, state } } as ClusterView,
  agents: {},
})
const CLUSTERS = [cluster('c1', 'alpha', 'working', 40), cluster('c2', 'bravo', 'waiting', 90)]

function setup(over: Partial<Parameters<typeof TopBar>[0]> = {}) {
  const props = { clusters: CLUSTERS, selectedId: 'c2', onSelect: vi.fn(), onNew: vi.fn(), onDiscard: vi.fn(), settingsCategories: [], ...over }
  render(<ThemeProvider theme={theme}><TopBar {...props} /></ThemeProvider>)
  return props
}

describe('TopBar', () => {
  test('tabs_one_per_cluster_in_slug_order', () => {
    const { onSelect } = setup()
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(['alpha1/140%', 'bravo1/190%'])
    expect(tabs[0].getAttribute('aria-selected')).toBe('false')
    expect(tabs[1].getAttribute('aria-selected')).toBe('true')
    expect(within(tabs[0]).getByRole('img', { name: 'state working' })).toBeTruthy()
    expect(within(tabs[1]).getByRole('img', { name: 'state waiting' })).toBeTruthy()
    expect(within(tabs[0]).getByLabelText('context 40%')).toBeTruthy()
    expect(within(tabs[1]).getByLabelText('context 90%')).toBeTruthy()
    expect(within(tabs[0]).getByTestId('context-gauge-tick')).toBeTruthy()
    fireEvent.click(tabs[0])
    expect(onSelect).toHaveBeenCalledWith('c1')
  })

  test('tab shows hook failed mark and ready count', () => {
    const failed = cluster('c3', 'charlie', 'error', null)
    failed.cluster = { ...failed.cluster, status: { discard_hook_failed: true }, summary: { ...failed.cluster.summary, ready: 2, replicas: 3 } } as ClusterView
    setup({ clusters: [...CLUSTERS, failed] })
    const tabs = screen.getAllByRole('tab')
    expect(within(tabs[2]).getByText('2/3')).toBeTruthy()
    expect(within(tabs[2]).getByLabelText('discard hook failed')).toBeTruthy()
    expect(within(tabs[0]).queryByLabelText('discard hook failed')).toBeNull()
    expect(within(tabs[0]).getByText('1/1')).toBeTruthy()
  })

  test('tab_actions_offer_discard_only', () => {
    const { onDiscard } = setup()
    fireEvent.click(screen.getAllByRole('button', { name: 'Cluster actions' })[0])
    const items = screen.getAllByRole('menuitem')
    expect(items.map((i) => i.textContent)).toEqual(['Discard'])
    expect(screen.queryByText('Edit')).toBeNull()
    fireEvent.click(items[0])
    expect(onDiscard).toHaveBeenCalledWith(CLUSTERS[0].cluster)
  })

  test('new_cluster_button', () => {
    const { onNew } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'New cluster' }))
    expect(onNew).toHaveBeenCalledTimes(1)
  })

  test('hamburger_lists_sessions_and_selects', () => {
    const { onSelect } = setup()
    expect(screen.queryByRole('presentation')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const list = screen.getByRole('list', { name: 'Sessions' })
    const items = within(list).getAllByRole('button')
    expect(items.map((i) => i.textContent)).toEqual(['alpha', 'bravo'])
    expect(items[1].getAttribute('aria-current')).toBe('true')
    expect(items[0].getAttribute('aria-current')).toBeNull()
    fireEvent.click(items[0])
    expect(onSelect).toHaveBeenCalledWith('c1')
  })

  test('hamburger_choosing_session_closes_drawer', async () => {
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    fireEvent.click(within(screen.getByRole('list', { name: 'Sessions' })).getAllByRole('button')[0])
    await vi.waitFor(() => expect(screen.queryByRole('list', { name: 'Sessions' })).toBeNull())
  })

  test('settings_section_bottom_with_empty_state', () => {
    const onOpen = vi.fn()
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    expect(screen.getByText('No settings yet')).toBeTruthy()
    cleanup()
    setup({ settingsCategories: [{ id: 'presets', label: 'Presets', onOpen }] })
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    expect(screen.queryByText('No settings yet')).toBeNull()
    const sessions = screen.getByRole('list', { name: 'Sessions' })
    const settings = screen.getByRole('region', { name: 'Settings' })
    expect(sessions.compareDocumentPosition(settings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(getComputedStyle(settings).marginTop).toBe('auto')
    fireEvent.click(within(settings).getByRole('button', { name: 'Presets' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })
})
