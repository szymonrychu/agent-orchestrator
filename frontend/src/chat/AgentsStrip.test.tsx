// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import type { AgentView, ClusterView } from '../api/client'
import { actionFromEvent, clustersReducer, initialState } from '../ribbon/useClusters'
import { theme } from '../theme'
import { AgentsStrip } from './AgentsStrip'
import { formatAge } from './agentAge'

const makeLeader = vi.hoisted(() => vi.fn())
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), makeLeader }))

afterEach(() => {
  makeLeader.mockReset()
  cleanup()
  vi.useRealTimers()
})

const agent = (id: string, ordinal: number, o: Partial<AgentView> = {}): AgentView => ({
  context: { used_percentage: 25, used_tokens: 1, window_tokens: 4 }, current_turn_id: null, free_since: null, gone: false, id, ordinal, leader: false, context_pct: null, peers: 0, pod_name: `alpha-${ordinal}`, ready: true, state: 'busy', ...o,
})

function mount(agents: AgentView[], selected: string[] = [], onToggle: (id: string) => void = () => {}, hive?: { clusterId: string; leaderEpoch: number | null }) {
  return render(
    <ThemeProvider theme={theme}>
      <AgentsStrip agents={agents} selected={selected} onToggle={onToggle} autoCompactPct={80} hive={hive} />
    </ThemeProvider>,
  )
}

describe('AgentsStrip', () => {
  test('shows_agent_rows', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:10:00Z'))
    const idle = agent('a1', 1, { state: 'idle', free_since: '2026-01-01T00:08:00Z', context: { used_percentage: 70, used_tokens: 1, window_tokens: 2 } })
    mount([agent('a0', 0), idle])
    const items = within(screen.getByRole('group', { name: 'agent filter' })).getAllByRole('button')
    expect(items.map((i) => i.getAttribute('aria-label'))).toEqual(['agent 0', 'agent 1'])
    const [a0, a1] = items
    expect(within(a0).getByText('alpha-0')).toBeTruthy()
    expect(within(a0).getByRole('img', { name: 'state working' })).toBeTruthy()
    expect(within(a0).getByRole('progressbar', { name: 'context 25%' })).toBeTruthy()
    expect(within(a0).queryByText(/free/)).toBeNull()
    expect(within(a1).getByText('alpha-1')).toBeTruthy()
    expect(within(a1).getByRole('img', { name: 'state waiting' })).toBeTruthy()
    expect(within(a1).getByText('free 2m')).toBeTruthy()
    expect(within(a1).getByRole('progressbar', { name: 'context 70%' })).toBeTruthy()
    // the free-since age refreshes on a one second clock
    act(() => {
      vi.advanceTimersByTime(60000)
    })
    expect(within(a1).getByText('free 3m')).toBeTruthy()
  })

  test('click_toggles_filter', () => {
    const onToggle = vi.fn()
    const { rerender } = mount([agent('a0', 0), agent('a1', 1)], [], onToggle)
    const group = screen.getByRole('group', { name: 'agent filter' })
    expect(within(group).getByRole('button', { name: 'agent 1' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(within(group).getByRole('button', { name: 'agent 1' }))
    expect(onToggle).toHaveBeenCalledWith('a1')
    rerender(
      <ThemeProvider theme={theme}>
        <AgentsStrip agents={[agent('a0', 0), agent('a1', 1)]} selected={['a1']} onToggle={onToggle} autoCompactPct={80} />
      </ThemeProvider>,
    )
    expect(within(group).getByRole('button', { name: 'agent 1' }).getAttribute('aria-pressed')).toBe('true')
    expect(within(group).getByRole('button', { name: 'agent 0' }).getAttribute('aria-pressed')).toBe('false')
  })

  test('format_age_unchanged', () => {
    expect([0, 45000, 59999, 60000, 120000, 3599000, 3600000, 86399000, 86400000, 172800000, -5000].map(formatAge)).toEqual(['0s', '45s', '59s', '1m', '2m', '59m', '1h', '23h', '1d', '2d', '0s'])
  })

  test('agent_filter_and_row_retired', () => {
    // Filesystem check via Vite's glob (keys only, nothing is imported), independent of git state.
    const onDisk = Object.keys(import.meta.glob(['./*', '../ribbon/*']))
    expect(onDisk).not.toContain('./AgentFilter.tsx')
    expect(onDisk).not.toContain('../ribbon/AgentRow.tsx')
    expect(onDisk).toContain('./AgentsStrip.tsx')
  })

  test('leader badge shows on the leader with the epoch tooltip and is absent on alone clusters', () => {
    const agents = [agent('a0', 0, { leader: true }), agent('a1', 1)]
    mount(agents, [], () => {}, { clusterId: 'c1', leaderEpoch: 7 })
    const badge = screen.getByText('leader')
    expect(badge.getAttribute('title')).toBe('leader epoch 7')
    expect(within(screen.getByTestId('agent-row-alpha-0')).getByText('leader')).toBeTruthy()
    expect(within(screen.getByTestId('agent-row-alpha-1')).queryByText('leader')).toBeNull()
    cleanup()
    mount(agents)
    expect(screen.queryByText('leader')).toBeNull()
    expect(screen.queryByRole('button', { name: /make leader/i })).toBeNull()
  })

  test('make leader calls the leader route for a non-leader agent and shows an error inline on failure', async () => {
    makeLeader.mockResolvedValueOnce({ request_id: 1 }).mockRejectedValueOnce(new Error('not a member'))
    mount([agent('a0', 0, { leader: true }), agent('a1', 1)], [], () => {}, { clusterId: 'c1', leaderEpoch: 1 })
    expect(screen.getAllByRole('button', { name: /make leader/i })).toHaveLength(1)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /make leader/i }))
    })
    expect(makeLeader).toHaveBeenCalledWith('c1', 'a1')
    expect(screen.queryByRole('alert')).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /make leader/i }))
    })
    expect(screen.getByRole('alert').textContent).toContain('not a member')
  })

  test('leader_changed frame moves the badge and context percent is shown per agent', () => {
    const cluster = { id: 'c1', slug: 'alpha', hive: true, leader_agent_id: 'a0', leader_epoch: 1 } as unknown as ClusterView
    const a0 = agent('a0', 0, { leader: true, context_pct: 41 })
    const a1 = agent('a1', 1, { context_pct: 63 })
    let state = clustersReducer(initialState, { type: 'snapshot', clusters: [{ cluster, agents: [a0, a1] }] })
    const view = () => {
      const e = state.clusters.c1
      return (
        <ThemeProvider theme={theme}>
          <AgentsStrip agents={Object.values(e.agents)} selected={[]} onToggle={() => {}} hive={{ clusterId: 'c1', leaderEpoch: e.cluster.leader_epoch }} />
        </ThemeProvider>
      )
    }
    const { rerender } = render(view())
    expect(within(screen.getByTestId('agent-row-alpha-0')).getByText('leader')).toBeTruthy()
    expect(within(screen.getByTestId('agent-row-alpha-0')).getByText('41%')).toBeTruthy()
    expect(within(screen.getByTestId('agent-row-alpha-1')).getByText('63%')).toBeTruthy()
    const action = actionFromEvent('leader_changed', { cluster_id: 'c1', agent_id: 'a1', epoch: 2 })
    expect(action).not.toBeNull()
    state = clustersReducer(state, action!)
    rerender(view())
    expect(within(screen.getByTestId('agent-row-alpha-1')).getByText('leader')).toBeTruthy()
    expect(within(screen.getByTestId('agent-row-alpha-0')).queryByText('leader')).toBeNull()
    expect(screen.getByText('leader').getAttribute('title')).toBe('leader epoch 2')
  })
})
