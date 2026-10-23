// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import type { AgentView, MessageView } from '../api/client'
import type { StreamOptions } from '../api/stream'
import { theme } from '../theme'
import { Composer } from './Composer'
import { QueueStrip } from './QueueStrip'

const api = vi.hoisted(() => ({ postMessage: vi.fn(), listMessages: vi.fn(), cancelMessage: vi.fn(), retryMessage: vi.fn() }))
const stream = vi.hoisted(() => ({ opened: [] as StreamOptions[], close: vi.fn() }))

vi.mock('../api/client', async (orig) => ({
  ...(await orig<typeof import('../api/client')>()),
  ...api,
  statusStreamUrl: () => '/api/v1/stream',
}))
vi.mock('../api/stream', () => ({
  openStream: (_url: string, opts: StreamOptions) => {
    stream.opened.push(opts)
    return { close: stream.close }
  },
}))

const agent = (ordinal: number): AgentView =>
  ({ id: `a${ordinal}`, ordinal, gone: false, ready: true, state: 'idle', peers: 0, pod_name: `p${ordinal}`, free_since: null, current_turn_id: null, context: null }) as AgentView
const agents = [agent(0), agent(1)]

const msg = (over: Partial<MessageView>): MessageView =>
  ({ id: 1, agent_id: null, attempts: 0, cluster_id: 'c1', created_at: '2026-01-01T00:00:00Z', dispatched_at: null, error: null, max_attempts: 5, next_attempt_at: null, status: 'queued', target_agent_id: null, text: 'hello', turn: null, turn_id: null, ...over }) as MessageView

const wrap = (ui: React.ReactNode) => render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>)

beforeEach(() => {
  stream.opened.length = 0
  api.postMessage.mockResolvedValue({ id: 9 })
  api.listMessages.mockResolvedValue([])
  api.cancelMessage.mockResolvedValue(undefined)
  api.retryMessage.mockResolvedValue({ id: 1 })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const input = () => screen.getByRole('textbox')

describe('Composer', () => {
  test('enter sends to any agent', async () => {
    wrap(<Composer clusterId="c1" agents={agents} deleting={false} />)
    fireEvent.change(input(), { target: { value: 'do it' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    await waitFor(() => expect(api.postMessage).toHaveBeenCalledWith('c1', { text: 'do it' }))
    await waitFor(() => expect((input() as HTMLTextAreaElement).value).toBe(''))
  })

  test('shift enter does not send', () => {
    wrap(<Composer clusterId="c1" agents={agents} deleting={false} />)
    fireEvent.change(input(), { target: { value: 'a' } })
    fireEvent.keyDown(input(), { key: 'Enter', shiftKey: true })
    expect(api.postMessage).not.toHaveBeenCalled()
  })

  test('sends to chosen agent', async () => {
    wrap(<Composer clusterId="c1" agents={agents} deleting={false} />)
    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(within(screen.getByRole('listbox')).getByText('Agent 1'))
    fireEvent.change(input(), { target: { value: 'hi' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(api.postMessage).toHaveBeenCalledWith('c1', { text: 'hi', target_agent_id: 'a1' }))
  })

  test('disables send when empty or deleting', () => {
    const { rerender } = wrap(<Composer clusterId="c1" agents={agents} deleting={false} />)
    const send = () => screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement
    expect(send().disabled).toBe(true)
    fireEvent.change(input(), { target: { value: '   ' } })
    expect(send().disabled).toBe(true)
    fireEvent.change(input(), { target: { value: 'x' } })
    expect(send().disabled).toBe(false)
    rerender(
      <ThemeProvider theme={theme}>
        <Composer clusterId="c1" agents={agents} deleting />
      </ThemeProvider>,
    )
    expect(send().disabled).toBe(true)
    fireEvent.keyDown(input(), { key: 'Enter' })
    expect(api.postMessage).not.toHaveBeenCalled()
  })

  test('shows api error', async () => {
    const { ApiError } = await vi.importActual<typeof import('../api/client')>('../api/client')
    api.postMessage.mockRejectedValue(new ApiError(409, 'cluster_deleting', 'cluster is being discarded'))
    wrap(<Composer clusterId="c1" agents={agents} deleting={false} />)
    fireEvent.change(input(), { target: { value: 'x' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    expect((await screen.findByRole('alert')).textContent).toContain('cluster is being discarded')
    expect((input() as HTMLTextAreaElement).value).toBe('x')
  })
})

describe('QueueStrip', () => {
  test('queue strip cancels and retries', async () => {
    api.listMessages.mockResolvedValue([msg({ id: 1, text: 'q one' }), msg({ id: 2, status: 'failed', text: 'f two', error: 'rejected by wrapper' })])
    wrap(<QueueStrip clusterId="c1" />)
    expect(await screen.findByText('q one')).toBeTruthy()
    expect(api.listMessages).toHaveBeenCalledWith('c1', { status: 'queued,verifying,failed' })
    expect(screen.getByText(/rejected by wrapper/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.cancelMessage).toHaveBeenCalledWith('1'))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.retryMessage).toHaveBeenCalledWith('2'))
  })

  test('queue strip is hidden when empty and refreshes on message_status', async () => {
    const { container } = wrap(<QueueStrip clusterId="c1" />)
    await waitFor(() => expect(api.listMessages).toHaveBeenCalledTimes(1))
    expect(container.textContent).toBe('')
    api.listMessages.mockResolvedValue([msg({ text: 'late' })])
    act(() => stream.opened[0].onEvent({ cluster_id: 'other', queued: 1, failed: 0 }, { type: 'message_status', id: '' }))
    expect(api.listMessages).toHaveBeenCalledTimes(1)
    act(() => stream.opened[0].onEvent({ cluster_id: 'c1', queued: 1, failed: 0 }, { type: 'message_status', id: '' }))
    expect(await screen.findByText('late')).toBeTruthy()
  })

  test('queue strip shows retry state', async () => {
    api.listMessages.mockResolvedValue([
      msg({ id: 1, text: 'waiting', attempts: 2, max_attempts: 5, next_attempt_at: '2026-01-01T00:05:00Z' }),
      msg({ id: 2, text: 'checking', status: 'verifying' }),
      msg({ id: 3, text: 'amb', status: 'failed', error: 'ambiguous: no answer from wrapper' }),
    ])
    wrap(<QueueStrip clusterId="c1" />)
    expect(await screen.findByText(/attempt 2 of 5/)).toBeTruthy()
    expect(screen.getByText(/next attempt/)).toBeTruthy()
    expect(screen.getByText(/checking delivery/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText(/may already have run/)).toBeTruthy()
    expect(api.retryMessage).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry anyway' }))
    await waitFor(() => expect(api.retryMessage).toHaveBeenCalledWith('3'))
  })
})
