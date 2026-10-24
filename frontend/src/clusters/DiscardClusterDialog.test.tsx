// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import type { ClusterView, Meta } from '../api/client'
import { theme } from '../theme'
import { DiscardClusterDialog } from './DiscardClusterDialog'

const api = vi.hoisted(() => ({ deleteCluster: vi.fn(), getMeta: vi.fn() }))
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), ...api }))

const meta = (o: Partial<Meta> = {}): Meta => ({
  agent_image: 'img', agent_images: ['img'], caps: { max_clusters: 8, max_pvc_size: '100Gi', max_replicas: 16 },
  defaults: {
    any_mcp_config: { plugins: ['x'] }, auto_compact_pct: 80, discard_hook: 'none', effort: 'high', max_attempts: 5, model: 'opus', pvc_size: '10Gi', replicas: 1,
    resources: { limits: { cpu: '4', memory: '8Gi' }, requests: { cpu: '500m', memory: '1Gi' } }, storage_class: 'rook-ceph-rwx', workdir: 'workdir',
  },
  discard_hook: 'none', models: ['opus', 'sonnet', 'haiku'], efforts: ['low', 'medium', 'high', 'max'], ...o,
})
const existing = (o: Partial<ClusterView['spec']> = {}): ClusterView =>
  ({ created_at: '2026-01-01T00:00:00Z', desired: 'active', generation: 1, id: 'c-1', observed_generation: 1, slug: 'alpha', status: {},
    spec: { env: { A: 'b' }, max_attempts: 5, peers: true, replicas: 2, workdir: 'work', pvc_size: '20Gi', model: 'opus', ...o },
    summary: { auto_compact_pct: 80, context_pct_max: null, failed: 0, queued: 0, ready: 0, replicas: 2, state: 'waiting' } }) as ClusterView

const wrap = (el: React.ReactElement) => render(<ThemeProvider theme={theme}>{el}</ThemeProvider>)
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  api.getMeta.mockResolvedValue(meta())
})
afterEach(cleanup)

describe('DiscardClusterDialog', () => {
  test('discard requires typed slug', async () => {
    api.deleteCluster.mockResolvedValue(existing())
    const onDiscarded = vi.fn()
    wrap(<DiscardClusterDialog open cluster={existing()} onClose={vi.fn()} onDiscarded={onDiscarded} />)
    expect(screen.getByText(/workspace PVC/)).toBeTruthy()
    expect(screen.getByText(/whole chat history/)).toBeTruthy()
    expect(screen.getByText(/no undo/i)).toBeTruthy()
    expect(button('Discard').disabled).toBe(true)
    await userEvent.type(field('Type the slug to confirm'), 'alph')
    expect(button('Discard').disabled).toBe(true)
    await userEvent.type(field('Type the slug to confirm'), 'a')
    expect(button('Discard').disabled).toBe(false)
    await userEvent.click(button('Discard'))
    await waitFor(() => expect(api.deleteCluster).toHaveBeenCalledWith('c-1'))
    await waitFor(() => expect(onDiscarded).toHaveBeenCalled())
  })

  test('discard names the export hook', async () => {
    api.getMeta.mockResolvedValue(meta({ discard_hook: 'url' }))
    wrap(<DiscardClusterDialog open cluster={existing({ discard_hook_url: 'https://hook.example/x' })} onClose={vi.fn()} onDiscarded={vi.fn()} />)
    expect(await screen.findByText(/exported to https:\/\/hook\.example\/x first/)).toBeTruthy()
    expect(screen.getByText(/failing hook holds the discard/)).toBeTruthy()
    cleanup()
    wrap(<DiscardClusterDialog open cluster={existing()} onClose={vi.fn()} onDiscarded={vi.fn()} />)
    expect(await screen.findByText(/exported to the configured url hook first/)).toBeTruthy()
    cleanup()
    api.getMeta.mockResolvedValue(meta())
    wrap(<DiscardClusterDialog open cluster={existing()} onClose={vi.fn()} onDiscarded={vi.fn()} />)
    await waitFor(() => expect(api.getMeta).toHaveBeenCalledTimes(3))
    expect(screen.queryByText(/exported to/)).toBeNull()
  })
})
