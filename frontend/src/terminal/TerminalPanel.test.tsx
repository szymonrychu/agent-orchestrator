// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { AgentView, ClusterView } from '../api/client'
import type { TerminalView } from '../api/terminals'
import { terminalTheme, theme } from '../theme'
import { TerminalPanel } from './TerminalPanel'
import type { TerminalSocketOptions } from './terminalSocket'

const api = vi.hoisted(() => ({ createTerminal: vi.fn(), listTerminals: vi.fn(), deleteTerminal: vi.fn() }))
const sockets = vi.hoisted(() => ({
  all: [] as { url: string; opts: TerminalSocketOptions; send: ReturnType<typeof vi.fn>; resize: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> }[],
}))
const xterm = vi.hoisted(() => ({
  terminals: [] as {
    cols: number
    rows: number
    options: unknown
    writes: (string | Uint8Array)[]
    resets: number
    disposed: boolean
    opened: HTMLElement | null
    fits: number
    typed: ((data: string) => void) | null
  }[],
  fitSize: { cols: 100, rows: 30 },
  hostSize: { width: 800, height: 600 },
  log: [] as string[],
}))

vi.mock('../api/terminals', async (orig) => ({ ...(await orig<typeof import('../api/terminals')>()), ...api }))
vi.mock('./terminalSocket', () => ({
  openTerminalSocket: (url: string, opts: TerminalSocketOptions) => {
    const s = { url, opts, send: vi.fn(), resize: vi.fn(), close: vi.fn() }
    sockets.all.push(s)
    return s
  },
}))
vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 80
    rows = 24
    writes: (string | Uint8Array)[] = []
    resets = 0
    disposed = false
    opened: HTMLElement | null = null
    fits = 0
    typed: ((data: string) => void) | null = null
    constructor(public options: unknown) {
      xterm.terminals.push(this)
    }
    loadAddon(addon: { attach?: (t: unknown) => void }) {
      addon.attach?.(this)
    }
    open(el: HTMLElement) {
      this.opened = el
    }
    write(data: string | Uint8Array) {
      this.writes.push(data)
      xterm.log.push('write')
    }
    writeln(data: string) {
      this.writes.push(`${data}\n`)
      xterm.log.push('write')
    }
    reset() {
      this.resets += 1
      xterm.log.push('reset')
    }
    onData(cb: (d: string) => void) {
      this.typed = cb
      return { dispose() {} }
    }
    focus() {}
    dispose() {
      this.disposed = true
    }
  },
}))
vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    term: { cols: number; rows: number; fits: number } | null = null
    attach(t: never) {
      this.term = t
    }
    fit() {
      if (!this.term) return
      this.term.fits += 1
      this.term.cols = xterm.fitSize.cols
      this.term.rows = xterm.fitSize.rows
    }
  },
}))

let observers: { cb: () => void; target: Element | null }[] = []
class FakeResizeObserver {
  entry: { cb: () => void; target: Element | null }
  constructor(cb: () => void) {
    this.entry = { cb, target: null }
    observers.push(this.entry)
  }
  observe(el: Element) {
    this.entry.target = el
  }
  unobserve() {}
  disconnect() {
    observers = observers.filter((o) => o !== this.entry)
  }
}

const CLUSTER = { id: 'c1', slug: 'alpha' } as ClusterView
const agent = (id: string, ordinal: number, ready = true): AgentView =>
  ({ id, ordinal, pod_name: `alpha-${ordinal}`, ready, gone: false, state: 'idle', peers: 0, context: null, current_turn_id: null, free_since: null }) as AgentView
const AGENTS = [agent('a0', 0, false), agent('a1', 1), agent('a2', 2)]
const shell = (terminal_id: string, agent_id = 'a1', alive = true): TerminalView => ({
  agent_id, alive, attached: false, cluster_id: 'c1', cols: 80, created_at: '2026-10-23T00:00:00Z', exit_code: alive ? null : 0, pod_name: `alpha-${agent_id.slice(1)}`, rows: 24, terminal_id,
})

function mount(agents = AGENTS) {
  return render(
    <ThemeProvider theme={theme}>
      <TerminalPanel cluster={CLUSTER} agents={agents} />
    </ThemeProvider>,
  )
}
const tabs = () => screen.queryAllByRole('tab')
const lastSocket = () => sockets.all[sockets.all.length - 1]
const flush = () => act(async () => {})

beforeEach(() => {
  observers = []
  sockets.all = []
  xterm.terminals = []
  xterm.log = []
  xterm.fitSize = { cols: 100, rows: 30 }
  xterm.hostSize = { width: 800, height: 600 }
  // jsdom has no layout: report the host's client size from the test (0 x 0 is a display:none host)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => xterm.hostSize.width)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => xterm.hostSize.height)
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  api.listTerminals.mockResolvedValue({ terminals: [], unavailable_agents: [] })
  api.deleteTerminal.mockResolvedValue(undefined)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('TerminalPanel', () => {
  test('reattaches_existing_shells', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111'), shell('bbbbbb222', 'a2')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    expect(api.listTerminals).toHaveBeenCalledWith('c1')
    expect(tabs().map((t) => t.textContent)).toEqual(['alpha-1 aaaaaa', 'alpha-2 bbbbbb'])
    expect(sockets.all.map((s) => s.url)).toEqual([
      `ws://${location.host}/api/v1/agents/a1/terminals/aaaaaa111/attach`,
      `ws://${location.host}/api/v1/agents/a2/terminals/bbbbbb222/attach`,
    ])
    expect(xterm.terminals[0].options).toMatchObject({ theme: terminalTheme })
    expect(api.createTerminal).not.toHaveBeenCalled()
  })

  test('empty_state_offers_new_terminal', async () => {
    mount()
    await flush()
    expect(screen.getByText('No terminals')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'New terminal' })).toBeTruthy()
  })

  test('new_terminal_on_chosen_agent', async () => {
    api.createTerminal.mockResolvedValue(shell('cccccc333', 'a2'))
    mount()
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'New terminal' }))
    const items = screen.getAllByRole('menuitem')
    expect(items.map((i) => i.textContent)).toEqual(['alpha-0', 'alpha-1', 'alpha-2'])
    expect(items[0].getAttribute('aria-disabled')).toBe('true')
    expect(items[1].classList.contains('Mui-selected')).toBe(true)
    expect(items[2].classList.contains('Mui-selected')).toBe(false)
    fireEvent.click(items[2])
    await waitFor(() => expect(tabs()).toHaveLength(1))
    expect(api.createTerminal).toHaveBeenCalledWith('a2')
    expect(tabs()[0].textContent).toBe('alpha-2 cccccc')
    expect(lastSocket().url).toContain('/agents/a2/terminals/cccccc333/attach')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  test('multiple_tabs_and_close', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111'), shell('bbbbbb222', 'a2')], unavailable_agents: [] })
    api.createTerminal.mockResolvedValue(shell('cccccc333'))
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    fireEvent.click(screen.getByRole('button', { name: 'New terminal' }))
    fireEvent.click(screen.getAllByRole('menuitem')[1])
    await waitFor(() => expect(tabs()).toHaveLength(3))
    // the new shell is the active tab, the others stay attached
    expect(tabs()[2].getAttribute('aria-selected')).toBe('true')
    expect(sockets.all.every((s) => s.close.mock.calls.length === 0)).toBe(true)
    fireEvent.click(tabs()[0])
    expect(tabs()[0].getAttribute('aria-selected')).toBe('true')
    expect(sockets.all.every((s) => s.close.mock.calls.length === 0)).toBe(true)
    fireEvent.click(within(tabs()[1]).getByRole('button', { name: 'Close alpha-2 bbbbbb' }))
    await waitFor(() => expect(tabs()).toHaveLength(2))
    expect(api.deleteTerminal).toHaveBeenCalledWith('a2', 'bbbbbb222')
    expect(tabs().map((t) => t.textContent)).toEqual(['alpha-1 aaaaaa', 'alpha-1 cccccc'])
    expect(sockets.all[1].close).toHaveBeenCalled()
  })

  test('typing_goes_to_the_socket_and_output_to_the_screen', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    act(() => xterm.terminals[0].typed?.('ls\r'))
    expect(lastSocket().send).toHaveBeenCalledWith('ls\r')
    const bytes = new Uint8Array([104, 105])
    act(() => lastSocket().opts.onData(bytes))
    expect(xterm.terminals[0].writes).toContain(bytes)
  })

  test('fit_on_resize_and_reset_on_reconnect', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    const term = xterm.terminals[0]
    const sock = lastSocket()
    // fitted on mount and the size reported
    expect(term.fits).toBeGreaterThan(0)
    expect(sock.resize).toHaveBeenLastCalledWith(100, 30)
    // a panel drag, minimize or maximize resizes the container
    const fitsBefore = term.fits
    xterm.fitSize = { cols: 132, rows: 40 }
    const host = observers.find((o) => o.target?.getAttribute('data-testid') === 'xterm-host')
    expect(host).toBeTruthy()
    act(() => host!.cb())
    expect(term.fits).toBe(fitsBefore + 1)
    expect(sock.resize).toHaveBeenLastCalledWith(132, 40)
    // first open: no reset; every later open resets before the replay
    act(() => sock.opts.onStatus('open'))
    act(() => sock.opts.onData(new Uint8Array([1])))
    expect(term.resets).toBe(0)
    act(() => sock.opts.onStatus('reconnecting'))
    expect(term.resets).toBe(0)
    act(() => sock.opts.onStatus('open'))
    act(() => sock.opts.onData(new Uint8Array([2])))
    expect(term.resets).toBe(1)
    expect(xterm.log.slice(-2)).toEqual(['reset', 'write'])
  })

  test('fit_skipped_while_host_hidden', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    const term = xterm.terminals[0]
    const sock = lastSocket()
    const host = observers.find((o) => o.target?.getAttribute('data-testid') === 'xterm-host')!
    // an inactive tab or a minimized panel is display:none: the host has no client size, so no fit and no resize
    xterm.hostSize = { width: 0, height: 0 }
    xterm.fitSize = { cols: 10, rows: 5 }
    const fitsBefore = term.fits
    const resizesBefore = sock.resize.mock.calls.length
    act(() => host.cb())
    expect(term.fits).toBe(fitsBefore)
    expect(sock.resize.mock.calls).toHaveLength(resizesBefore)
    // shown again: fitted and the size reported
    xterm.hostSize = { width: 800, height: 600 }
    xterm.fitSize = { cols: 120, rows: 36 }
    act(() => host.cb())
    expect(term.fits).toBe(fitsBefore + 1)
    expect(sock.resize).toHaveBeenLastCalledWith(120, 36)
  })

  test('exit_prints_a_notice_and_keeps_the_tab', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    act(() => lastSocket().opts.onExit(3))
    act(() => lastSocket().opts.onStatus('closed'))
    expect(xterm.terminals[0].writes.join('')).toContain('exited with code 3')
    expect(tabs()).toHaveLength(1)
    expect(api.deleteTerminal).not.toHaveBeenCalled()
  })

  test('errors_inline', async () => {
    api.createTerminal.mockRejectedValueOnce(new ApiError(409, 'terminal_limit', 'too many terminals'))
    mount()
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'New terminal' }))
    fireEvent.click(screen.getAllByRole('menuitem')[1])
    expect((await screen.findByRole('alert')).textContent).toContain('terminal_limit: too many terminals')
    expect(tabs()).toHaveLength(0)
    api.createTerminal.mockRejectedValueOnce(new ApiError(403, 'forbidden_origin', 'origin not allowed'))
    fireEvent.click(screen.getByRole('button', { name: 'New terminal' }))
    fireEvent.click(screen.getAllByRole('menuitem')[1])
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('forbidden_origin'))
    api.createTerminal.mockRejectedValueOnce(new ApiError(409, 'agent_not_ready', 'agent is not ready'))
    fireEvent.click(screen.getByRole('button', { name: 'New terminal' }))
    fireEvent.click(screen.getAllByRole('menuitem')[2])
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('agent_not_ready'))
  })

  test('errors_inline_on_listing', async () => {
    api.listTerminals.mockRejectedValue(new ApiError(502, 'upstream_error', 'wrapper unreachable'))
    mount()
    expect((await screen.findByRole('alert')).textContent).toContain('upstream_error: wrapper unreachable')
  })

  test('reopens_only_alive_shells', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111'), shell('dead00999', 'a1', false), shell('bbbbbb222', 'a2')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    expect(tabs().map((t) => t.textContent)).toEqual(['alpha-1 aaaaaa', 'alpha-2 bbbbbb'])
    expect(sockets.all).toHaveLength(2)
  })

  test('refused_tab_retry_and_close', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    act(() => lastSocket().opts.onStatus('refused'))
    expect(screen.getByTestId('terminal-refused').textContent).toContain('refused')
    expect(tabs()).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(sockets.all).toHaveLength(2)
    expect(sockets.all[0].close).toHaveBeenCalled()
    expect(sockets.all[1].url).toBe(sockets.all[0].url)
    expect(screen.queryByTestId('terminal-refused')).toBeNull()
    act(() => lastSocket().opts.onStatus('refused'))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(tabs()).toHaveLength(0))
    expect(api.deleteTerminal).toHaveBeenCalledWith('a1', 'aaaaaa111')
  })

  test('remount_reattaches_with_replay', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    const first = mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    first.unmount()
    expect(sockets.all[0].close).toHaveBeenCalled()
    expect(xterm.terminals[0].disposed).toBe(true)
    expect(api.deleteTerminal).not.toHaveBeenCalled()
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    expect(sockets.all).toHaveLength(2)
    expect(sockets.all[1].url).toBe(sockets.all[0].url)
    const replay = new Uint8Array([72, 105])
    act(() => sockets.all[1].opts.onData(replay))
    expect(xterm.terminals[1].writes).toContain(replay)
  })

  test('cluster_change_lists_that_clusters_shells', async () => {
    api.listTerminals.mockResolvedValueOnce({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    const view = mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    api.listTerminals.mockResolvedValueOnce({ terminals: [], unavailable_agents: [] })
    view.rerender(
      <ThemeProvider theme={theme}>
        <TerminalPanel cluster={{ id: 'c2', slug: 'beta' } as ClusterView} agents={[]} />
      </ThemeProvider>,
    )
    await waitFor(() => expect(tabs()).toHaveLength(0))
    expect(api.listTerminals).toHaveBeenLastCalledWith('c2')
    expect(sockets.all[0].close).toHaveBeenCalled()
    expect(api.deleteTerminal).not.toHaveBeenCalled()
  })

  test('relists when ready agents change and shows unavailable agents', async () => {
    api.listTerminals.mockResolvedValueOnce({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    const view = mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    expect(api.listTerminals).toHaveBeenCalledTimes(1)
    // a status delta that leaves the ready set alone does not list again
    view.rerender(
      <ThemeProvider theme={theme}>
        <TerminalPanel cluster={CLUSTER} agents={AGENTS.map((a) => ({ ...a }))} />
      </ThemeProvider>,
    )
    await flush()
    expect(api.listTerminals).toHaveBeenCalledTimes(1)
    // agent a0 becomes ready late: its shells appear, existing tabs stay
    api.listTerminals.mockResolvedValueOnce({ terminals: [shell('aaaaaa111'), shell('dddddd444', 'a0')], unavailable_agents: ['a2'] })
    view.rerender(
      <ThemeProvider theme={theme}>
        <TerminalPanel cluster={CLUSTER} agents={[agent('a0', 0), agent('a1', 1), agent('a2', 2)]} />
      </ThemeProvider>,
    )
    await waitFor(() => expect(tabs()).toHaveLength(2))
    expect(api.listTerminals).toHaveBeenCalledTimes(2)
    expect(tabs().map((t) => t.textContent)).toEqual(['alpha-1 aaaaaa', 'alpha-0 dddddd'])
    expect(sockets.all).toHaveLength(2)
    expect(screen.getByTestId('terminal-unavailable').textContent).toBe('shells on alpha-2 unavailable')
    // the notice clears once the next listing has no unavailable agents
    api.listTerminals.mockResolvedValueOnce({ terminals: [shell('aaaaaa111'), shell('dddddd444', 'a0')], unavailable_agents: [] })
    view.rerender(
      <ThemeProvider theme={theme}>
        <TerminalPanel cluster={CLUSTER} agents={[agent('a0', 0), agent('a1', 1), agent('a2', 2, false)]} />
      </ThemeProvider>,
    )
    await waitFor(() => expect(screen.queryByTestId('terminal-unavailable')).toBeNull())
    expect(api.listTerminals).toHaveBeenCalledTimes(3)
  })

  test('a listing in flight before a close does not bring the closed shell back', async () => {
    api.listTerminals.mockResolvedValueOnce({ terminals: [shell('aaaaaa111'), shell('bbbbbb222', 'a2')], unavailable_agents: [] })
    const view = mount()
    await waitFor(() => expect(tabs()).toHaveLength(2))
    // A relist starts (a late agent became ready) and stays pending across the close.
    let resolveList!: (v: { terminals: TerminalView[]; unavailable_agents: string[] }) => void
    api.listTerminals.mockReturnValueOnce(new Promise((r) => (resolveList = r)))
    view.rerender(
      <ThemeProvider theme={theme}>
        <TerminalPanel cluster={CLUSTER} agents={[agent('a0', 0), agent('a1', 1), agent('a2', 2)]} />
      </ThemeProvider>,
    )
    await flush()
    expect(api.listTerminals).toHaveBeenCalledTimes(2)
    let resolveDelete!: () => void
    api.deleteTerminal.mockReturnValueOnce(new Promise<void>((r) => (resolveDelete = r)))
    fireEvent.click(within(tabs()[0]).getByRole('button', { name: 'Close alpha-1 aaaaaa' }))
    await flush()
    resolveDelete()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    // The stale listing, taken before the close, still names the closed shell.
    await act(async () => resolveList({ terminals: [shell('aaaaaa111'), shell('bbbbbb222', 'a2'), shell('dddddd444', 'a0')], unavailable_agents: [] }))
    expect(tabs().map((t) => t.textContent)).toEqual(['alpha-2 bbbbbb', 'alpha-0 dddddd'])
  })

  test('refused tab shows the socket reason', async () => {
    api.listTerminals.mockResolvedValue({ terminals: [shell('aaaaaa111')], unavailable_agents: [] })
    mount()
    await waitFor(() => expect(tabs()).toHaveLength(1))
    act(() => lastSocket().opts.onStatus('refused', 'connection refused before open (check ORCH_ALLOWED_ORIGINS)'))
    expect(screen.getByTestId('terminal-refused').textContent).toContain('check ORCH_ALLOWED_ORIGINS')
  })
})
