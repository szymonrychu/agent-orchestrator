// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { ClusterView, Meta } from '../api/client'
import { theme } from '../theme'
import { ClusterForm } from './ClusterForm'
import { DiscardClusterDialog } from './DiscardClusterDialog'

const api = vi.hoisted(() => ({ createCluster: vi.fn(), patchCluster: vi.fn(), deleteCluster: vi.fn(), getMeta: vi.fn() }))
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

async function ready() {
  await waitFor(() => expect(field('Max attempts').value).toBe('5'))
}
const replace = async (label: string, text: string) => {
  await userEvent.clear(field(label))
  if (text) await userEvent.type(field(label), text)
}
const pick = async (label: string, option: string) => {
  await userEvent.click(screen.getByRole('combobox', { name: label }))
  await userEvent.click(screen.getByRole('option', { name: option }))
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
}
const paste = async (label: string, text: string) => {
  await userEvent.click(field(label))
  await userEvent.paste(text)
}

describe('ClusterForm', () => {
  test('creates cluster with full spec', async () => {
    api.createCluster.mockResolvedValue(existing())
    const onSaved = vi.fn()
    wrap(<ClusterForm open mode="create" onClose={vi.fn()} onSaved={onSaved} />)
    await ready()
    await userEvent.type(field('Slug'), 'alpha-1')
    await replace('Replicas', '3')
    await pick('Model', 'sonnet')
    await pick('Effort', 'low')
    await userEvent.click(field('Peers'))
    await replace('Workdir', 'ws')
    await replace('PVC size', '5Gi')
    await userEvent.click(button('Add env'))
    await userEvent.type(field('Env key 1'), 'FOO')
    await userEvent.type(field('Env value 1'), 'bar')
    await userEvent.clear(field('any-mcp config'))
    await paste('any-mcp config', '{"plugins": []}')
    await replace('Max attempts', '4')
    await replace('Auto-compact percent', '70')
    await replace('CPU request', '1')
    await replace('CPU limit', '2')
    await replace('Memory request', '1Gi')
    await replace('Memory limit', '4Gi')
    await userEvent.type(field('Pre-discard webhook URL'), 'https://hook.example/x')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createCluster).toHaveBeenCalledTimes(1))
    expect(api.createCluster).toHaveBeenCalledWith({
      slug: 'alpha-1',
      spec: {
        replicas: 3, model: 'sonnet', effort: 'low', peers: false, workdir: 'ws', pvc_size: '5Gi', env: { FOO: 'bar' },
        any_mcp_config: { plugins: [] }, max_attempts: 4, auto_compact_pct: 70,
        resources: { requests: { cpu: '1', memory: '1Gi' }, limits: { cpu: '2', memory: '4Gi' } },
        discard_hook_url: 'https://hook.example/x',
      },
    })
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  test('model and effort are selects fed by meta, create prefills every default', async () => {
    wrap(<ClusterForm open mode="create" onClose={vi.fn()} onSaved={vi.fn()} />)
    await ready()
    expect(screen.getByRole('combobox', { name: 'Model' }).textContent).toBe('opus')
    expect(screen.getByRole('combobox', { name: 'Effort' }).textContent).toBe('high')
    await userEvent.click(screen.getByRole('combobox', { name: 'Model' }))
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['default', 'opus', 'sonnet', 'haiku'])
    await userEvent.keyboard('{Escape}')
    await userEvent.click(screen.getByRole('combobox', { name: 'Effort' }))
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['model default', 'low', 'medium', 'high', 'max'])
    await userEvent.keyboard('{Escape}')
    expect(field('Replicas').value).toBe('1')
    expect(field('Workdir').value).toBe('workdir')
    expect(field('PVC size').value).toBe('10Gi')
    expect(field('any-mcp config').value).toBe(JSON.stringify({ plugins: ['x'] }, null, 2))
    expect(field('Auto-compact percent').value).toBe('80')
    expect(field('CPU request').value).toBe('500m')
    expect(field('CPU limit').value).toBe('4')
    expect(field('Memory request').value).toBe('1Gi')
    expect(field('Memory limit').value).toBe('8Gi')
  })

  test('edit keeps a model outside the list as an extra option', async () => {
    wrap(<ClusterForm open mode="edit" cluster={existing({ model: 'legacy-model', effort: null })} onClose={vi.fn()} onSaved={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' }).textContent).toBe('legacy-model'))
    await userEvent.click(screen.getByRole('combobox', { name: 'Model' }))
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['default', 'legacy-model', 'opus', 'sonnet', 'haiku'])
  })

  test('edit with a null model shows the default option', async () => {
    wrap(<ClusterForm open mode="edit" cluster={existing({ model: null, effort: null })} onClose={vi.fn()} onSaved={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' }).textContent).toBe('default'))
  })

  test('validates slug and env keys', async () => {
    wrap(<ClusterForm open mode="create" onClose={vi.fn()} onSaved={vi.fn()} />)
    await ready()
    await userEvent.type(field('Slug'), 'Bad_Slug')
    expect(screen.getByText(/lowercase letters, digits and hyphens/)).toBeTruthy()
    await replace('Slug', 'a'.repeat(41))
    expect(screen.getByText(/at most 40/)).toBeTruthy()
    await replace('Slug', 'ok')
    await userEvent.click(button('Add env'))
    await userEvent.type(field('Env key 1'), 'API_TOKEN')
    expect(screen.getByText(/secret/i)).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await replace('Env key 1', 'CCW_X')
    expect(screen.getByText(/reserved/i)).toBeTruthy()
    await replace('Env key 1', 'GOOD')
    expect(button('Create').disabled).toBe(false)
    await userEvent.type(field('Pre-discard webhook URL'), 'ftp://x')
    expect(screen.getByText(/http or https/)).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await replace('Pre-discard webhook URL', '')
    await paste('any-mcp config', '{nope')
    expect(screen.getByText(/valid JSON/)).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    expect(api.createCluster).not.toHaveBeenCalled()
  })

  test('edit patches spec', async () => {
    api.patchCluster.mockResolvedValue(existing())
    wrap(<ClusterForm open mode="edit" cluster={existing({ image: 'reg/custom:1' })} onClose={vi.fn()} onSaved={vi.fn()} />)
    await waitFor(() => expect(field('Replicas').value).toBe('2'))
    expect(field('Slug').disabled).toBe(true)
    expect(field('PVC size').disabled).toBe(true)
    expect(field('PVC size').value).toBe('20Gi')
    expect(field('Env key 1').value).toBe('A')
    await replace('Replicas', '4')
    await userEvent.click(button('Save'))
    await waitFor(() => expect(api.patchCluster).toHaveBeenCalledTimes(1))
    const [id, body] = api.patchCluster.mock.calls[0]
    expect(body.spec.image).toBe('reg/custom:1')
    expect(id).toBe('c-1')
    expect(body.spec).toMatchObject({ replicas: 4, pvc_size: '20Gi', workdir: 'work', env: { A: 'b' }, model: 'opus' })
    expect(body).not.toHaveProperty('slug')
    expect(api.createCluster).not.toHaveBeenCalled()
  })

  test('shows server errors', async () => {
    api.createCluster.mockRejectedValueOnce(new ApiError(409, 'conflict', 'slug alpha already exists'))
    wrap(<ClusterForm open mode="create" onClose={vi.fn()} onSaved={vi.fn()} />)
    await ready()
    await userEvent.type(field('Slug'), 'alpha')
    await userEvent.click(button('Create'))
    expect(await screen.findByText('slug alpha already exists')).toBeTruthy()
    api.createCluster.mockRejectedValueOnce(new ApiError(422, 'invalid', 'replicas above cap'))
    await userEvent.click(button('Create'))
    expect(await screen.findByText('replicas above cap')).toBeTruthy()
    expect(screen.queryByText('slug alpha already exists')).toBeNull()
  })
})

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

describe('Ribbon wiring', () => {
  test('ribbon opens create, edit and discard', async () => {
    const { Ribbon } = await import('../ribbon/Ribbon')
    const entry = { cluster: { ...existing(), status: { discard_hook_failed: true } }, agents: {} }
    wrap(<Ribbon clusters={[entry]} selectedId={null} onSelect={vi.fn()} />)
    expect(screen.getByText('hook failed')).toBeTruthy()
    await userEvent.click(button('New cluster'))
    expect(await screen.findByLabelText('Slug')).toBeTruthy()
    await userEvent.click(button('Cancel'))
    await waitFor(() => expect(screen.queryByLabelText('Slug')).toBeNull())
    await userEvent.click(button('actions alpha'))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Edit' }))
    await waitFor(() => expect(field('Replicas').value).toBe('2'))
    await userEvent.click(button('Cancel'))
    await waitFor(() => expect(screen.queryByLabelText('Slug')).toBeNull())
    await userEvent.click(button('actions alpha'))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Discard' }))
    expect(await screen.findByLabelText('Type the slug to confirm')).toBeTruthy()
  })
})
