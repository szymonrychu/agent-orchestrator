// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import type { AgentView } from '../api/client'
import { theme } from '../theme'
import { AgentsStrip } from './AgentsStrip'
import { formatAge } from './agentAge'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const agent = (id: string, ordinal: number, o: Partial<AgentView> = {}): AgentView => ({
  context: { used_percentage: 25, used_tokens: 1, window_tokens: 4 }, current_turn_id: null, free_since: null, gone: false, id, ordinal, peers: 0, pod_name: `alpha-${ordinal}`, ready: true, state: 'busy', ...o,
})

function mount(agents: AgentView[], selected: string[] = [], onToggle: (id: string) => void = () => {}) {
  return render(
    <ThemeProvider theme={theme}>
      <AgentsStrip agents={agents} selected={selected} onToggle={onToggle} autoCompactPct={80} />
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

  test('agent_filter_and_row_retired', async () => {
    // The frontend has no node typings, so the builtin is loaded through a variable specifier.
    const builtin = 'node:child_process'
    const { execFileSync } = (await import(/* @vite-ignore */ builtin)) as { execFileSync: (cmd: string, args: string[], opts: { encoding: 'utf8' }) => string }
    const files = execFileSync('git', ['ls-files', '--', 'src/chat', 'src/ribbon'], { encoding: 'utf8' }).split('\n')
    expect(files).not.toContain('src/chat/AgentFilter.tsx')
    expect(files).not.toContain('src/ribbon/AgentRow.tsx')
    expect(files).toContain('src/chat/AgentsStrip.tsx')
  })
})
