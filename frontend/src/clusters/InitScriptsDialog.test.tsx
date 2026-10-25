// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import type { ClusterView, InitScriptsView } from '../api/client'
import { theme } from '../theme'
import { InitScriptsDialog } from './InitScriptsDialog'

const api = vi.hoisted(() => ({ getInitScripts: vi.fn(), putInitScripts: vi.fn() }))
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), ...api }))

const cluster = { id: 'c-1', slug: 'alpha', spec: {}, status: {} } as unknown as ClusterView
const view = (o: Partial<InitScriptsView> = {}): InitScriptsView => ({
  cluster_id: 'c-1', edited_since_run: false, log_path: '/w/.agent-init/log', preset_id: 'p-1', preset_name: 'dev', preset_updated: false,
  script_path: '/w/.agent-init/scripts', scripts: ['echo one'], status: { scripts_hash: 'h', state: 'succeeded', exit_code: 0, started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:01:00Z', log_tail: 'tail line' }, ...o,
})
const wrap = () => render(<ThemeProvider theme={theme}><InitScriptsDialog open cluster={cluster} onClose={vi.fn()} /></ThemeProvider>)

beforeEach(() => Object.values(api).forEach((f) => f.mockReset()))
afterEach(cleanup)

describe('InitScriptsDialog', () => {
  test('states are rendered', async () => {
    api.getInitScripts.mockResolvedValue(view({ edited_since_run: true }))
    wrap()
    expect(await screen.findByText('succeeded')).toBeTruthy()
    expect(screen.getByText('tail line')).toBeTruthy()
    expect(screen.getByText('/w/.agent-init/scripts')).toBeTruthy()
    expect(screen.getByText(/edited since the run/i)).toBeTruthy()
    expect(screen.getByText(/exit code 0/i)).toBeTruthy()
    expect(api.getInitScripts).toHaveBeenCalledWith('c-1')
    cleanup()
    api.getInitScripts.mockResolvedValue(view({ status: null }))
    wrap()
    expect(await screen.findByText(/no run recorded/i)).toBeTruthy()
    cleanup()
    api.getInitScripts.mockResolvedValue(view({ status: { scripts_hash: 'h', state: 'skipped' } }))
    wrap()
    expect(await screen.findByText('skipped')).toBeTruthy()
    cleanup()
    api.getInitScripts.mockResolvedValue(view({ status: { scripts_hash: 'h', state: 'running', started_at: '2026-01-01T00:00:00Z' } }))
    wrap()
    expect(await screen.findByText('running')).toBeTruthy()
    cleanup()
    api.getInitScripts.mockResolvedValue(view({ status: { scripts_hash: 'h', state: 'failed', exit_code: 3, reason: 'boom' } }))
    wrap()
    expect(await screen.findByText('failed')).toBeTruthy()
    expect(screen.getByText(/exit code 3/i)).toBeTruthy()
    expect(screen.getByText(/boom/)).toBeTruthy()
  })

  test('save puts edited scripts and shows messages', async () => {
    api.getInitScripts.mockResolvedValue(view())
    api.putInitScripts.mockResolvedValue(view({ scripts: ['echo two'], preset_updated: true }))
    wrap()
    const box = await screen.findByLabelText('Init script 1')
    await userEvent.clear(box)
    await userEvent.type(box, 'echo two')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.putInitScripts).toHaveBeenCalledWith('c-1', ['echo two']))
    expect(await screen.findByText(/Saved\. Applies to the next cluster/)).toBeTruthy()
    expect(screen.getByText('Preset dev updated')).toBeTruthy()
  })

  test('save blocks an empty script and shows a server error in an Alert', async () => {
    api.getInitScripts.mockResolvedValue(view())
    api.putInitScripts.mockRejectedValue(new Error('server said no'))
    wrap()
    await screen.findByLabelText('Init script 1')
    await userEvent.click(screen.getByRole('button', { name: 'Add init script' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByText('Init script 2 must not be empty')).toBeTruthy()
    expect(api.putInitScripts).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Remove init script 2' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect((await screen.findByRole('alert')).textContent).toContain('server said no')
  })
})
