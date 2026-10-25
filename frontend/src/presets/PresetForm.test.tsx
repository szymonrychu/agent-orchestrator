// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { Meta, PresetView } from '../api/client'
import { theme } from '../theme'
import { PresetForm } from './PresetForm'

const api = vi.hoisted(() => ({ createPreset: vi.fn(), updatePreset: vi.fn(), getMeta: vi.fn() }))
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), ...api }))

const meta = (o: Partial<Meta> = {}): Meta => ({
  agent_image: 'img', agent_images: ['img'], caps: { max_clusters: 8, max_pvc_size: '100Gi', max_replicas: 16 },
  defaults: {
    any_mcp_config: { plugins: ['x'] }, auto_compact_pct: 80, discard_hook: 'none', effort: 'high', max_attempts: 5, model: 'opus', pvc_size: '10Gi', replicas: 1,
    resources: { limits: { cpu: '4', memory: '8Gi' }, requests: { cpu: '500m', memory: '1Gi' } }, storage_class: 'rook-ceph-rwx', workdir: 'workdir',
  },
  discard_hook: 'none', models: ['opus', 'sonnet', 'haiku'], efforts: ['low', 'medium', 'high', 'max'], ...o,
})
const preset = (o: Partial<PresetView> = {}): PresetView =>
  ({ created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', id: 'p-1', name: 'fast', is_default: false, spec: {}, ...o }) as PresetView

const wrap = (el: React.ReactElement) => render(<ThemeProvider theme={theme}>{el}</ThemeProvider>)
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  api.getMeta.mockResolvedValue(meta())
})
afterEach(cleanup)

async function ready() {
  await waitFor(() => expect(field('Auto-compact percent').value).toBe('80'))
}
const paste = async (label: string, text: string) => {
  await userEvent.click(field(label))
  await userEvent.paste(text)
}
const replace = async (label: string, text: string) => {
  await userEvent.clear(field(label))
  if (text) await paste(label, text)
}
const pick = async (label: string, option: string) => {
  await userEvent.click(screen.getByRole('combobox', { name: label }))
  await userEvent.click(screen.getByRole('option', { name: option }))
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
}


const noop = { onClose: vi.fn(), onSaved: vi.fn() }
const optionTexts = () => screen.getAllByRole('option').map((o) => o.textContent)

describe('PresetForm', () => {
  test('keeps edits made before meta loads', { timeout: 15000 }, async () => {
    let resolveMeta: (m: Meta) => void = () => {}
    api.getMeta.mockReturnValue(new Promise<Meta>((r) => { resolveMeta = r }))
    api.createPreset.mockResolvedValue(preset())
    wrap(<PresetForm open onClose={vi.fn()} onSaved={vi.fn()} />)
    await replace('Name', 'early')
    await replace('Max attempts', '9')
    resolveMeta(meta())
    await ready()
    expect(field('Name').value).toBe('early')
    expect(field('Max attempts').value).toBe('9')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalled())
    const body = api.createPreset.mock.calls[0][0]
    expect(body.name).toBe('early')
    expect(body.spec.max_attempts).toBe(9)
  })

  test('new_preset_prefills_defaults_saves_only_set_fields', { timeout: 15000 }, async () => {
    api.createPreset.mockResolvedValue(preset())
    const onSaved = vi.fn()
    wrap(<PresetForm open onClose={vi.fn()} onSaved={onSaved} />)
    await ready()
    expect(screen.getByRole('combobox', { name: 'Model' }).textContent).toBe('opus')
    expect(screen.getByRole('combobox', { name: 'Effort' }).textContent).toBe('high')
    await userEvent.click(screen.getByRole('combobox', { name: 'Model' }))
    expect(optionTexts()).toEqual(['default', 'opus', 'sonnet', 'haiku'])
    await userEvent.keyboard('{Escape}')
    await userEvent.click(screen.getByRole('combobox', { name: 'Effort' }))
    expect(optionTexts()).toEqual(['model default', 'low', 'medium', 'high', 'max'])
    await userEvent.keyboard('{Escape}')
    expect(field('Replicas').value).toBe('1')
    expect(field('Workdir').value).toBe('workdir')
    expect(field('PVC size').value).toBe('10Gi')
    expect(field('any-mcp config').value).toBe(JSON.stringify({ plugins: ['x'] }, null, 2))
    expect(field('Max attempts').value).toBe('5')
    expect(field('CPU request').value).toBe('500m')
    expect(field('CPU limit').value).toBe('4')
    expect(field('Memory request').value).toBe('1Gi')
    expect(field('Memory limit').value).toBe('8Gi')
    expect(button('Create').disabled).toBe(true)
    await paste('Name', 'Fast 1')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalledTimes(1))
    expect(api.createPreset).toHaveBeenLastCalledWith({ name: 'Fast 1', spec: {} })
    await waitFor(() => expect(onSaved).toHaveBeenCalled())

    await replace('Replicas', '3')
    await pick('Model', 'sonnet')
    await userEvent.click(field('Peers'))
    await userEvent.click(button('Add env'))
    await paste('Env key 1', 'FOO')
    await paste('Env value 1', 'bar')
    await replace('Memory limit', '4Gi')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalledTimes(2))
    expect(api.createPreset).toHaveBeenLastCalledWith({
      name: 'Fast 1',
      spec: {
        replicas: 3, model: 'sonnet', peers: false, env: { FOO: 'bar' },
        resources: { requests: { cpu: '500m', memory: '1Gi' }, limits: { cpu: '4', memory: '4Gi' } },
      },
    })
    expect(api.updatePreset).not.toHaveBeenCalled()
  })

  test('edit_round_trips_stored_fields', async () => {
    const stored = { replicas: 2, model: 'opus', env: { A: 'b' }, pvc_size: '20Gi', workdir: 'work', image: 'reg/custom:1' }
    api.updatePreset.mockResolvedValue(preset({ spec: stored }))
    wrap(<PresetForm open preset={preset({ spec: stored })} {...noop} />)
    await ready()
    expect(field('Name').value).toBe('fast')
    expect(field('Replicas').value).toBe('2')
    expect(field('Workdir').value).toBe('work')
    expect(field('PVC size').value).toBe('20Gi')
    expect(field('Env key 1').value).toBe('A')
    expect(field('Env value 1').value).toBe('b')
    expect(field('Max attempts').value).toBe('5')
    expect(screen.getByRole('combobox', { name: 'Effort' }).textContent).toBe('high')
    await userEvent.click(button('Save'))
    await waitFor(() => expect(api.updatePreset).toHaveBeenCalledTimes(1))
    expect(api.updatePreset).toHaveBeenLastCalledWith('p-1', { name: 'fast', spec: stored })
    await replace('Replicas', '4')
    await userEvent.click(button('Save'))
    await waitFor(() => expect(api.updatePreset).toHaveBeenCalledTimes(2))
    expect(api.updatePreset).toHaveBeenLastCalledWith('p-1', { name: 'fast', spec: { ...stored, replicas: 4 } })
    expect(api.createPreset).not.toHaveBeenCalled()
  })

  test('edit_shows_unavailable_stored_value', async () => {
    const stored = { model: 'legacy-model', effort: 'ultra' }
    api.updatePreset.mockResolvedValue(preset({ spec: stored }))
    wrap(<PresetForm open preset={preset({ spec: stored })} {...noop} />)
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Model' }).textContent).toBe('legacy-model (not available)'))
    expect(screen.getByRole('combobox', { name: 'Effort' }).textContent).toBe('ultra (not available)')
    await userEvent.click(screen.getByRole('combobox', { name: 'Model' }))
    expect(optionTexts()).toEqual(['default', 'legacy-model (not available)', 'opus', 'sonnet', 'haiku'])
    await userEvent.keyboard('{Escape}')
    await userEvent.click(button('Save'))
    await waitFor(() => expect(api.updatePreset).toHaveBeenCalledTimes(1))
    expect(api.updatePreset).toHaveBeenLastCalledWith('p-1', { name: 'fast', spec: stored })
  })

  test('clearing_a_field_unsets_it', async () => {
    api.updatePreset.mockResolvedValue(preset())
    const stored = { replicas: 2, pvc_size: '20Gi', max_attempts: 3, model: 'opus', workdir: 'work' }
    wrap(<PresetForm open preset={preset({ spec: stored })} {...noop} />)
    await ready()
    await userEvent.clear(field('Replicas'))
    await userEvent.clear(field('PVC size'))
    await userEvent.clear(field('Max attempts'))
    await userEvent.clear(field('Workdir'))
    await pick('Model', 'default')
    expect(button('Save').disabled).toBe(false)
    await userEvent.click(button('Save'))
    await waitFor(() => expect(api.updatePreset).toHaveBeenCalledTimes(1))
    const body = api.updatePreset.mock.calls[0][1]
    expect(body).toEqual({ name: 'fast', spec: {} })
    expect(JSON.stringify(body)).not.toContain('null')
  })

  test('copy_prefills_from_source', async () => {
    const spec = { replicas: 2, model: 'opus' }
    api.createPreset.mockResolvedValue(preset())
    wrap(<PresetForm open copyOf={preset({ name: 'fast', spec, is_default: true })} {...noop} />)
    await ready()
    expect(field('Name').value).toBe('fast copy')
    expect(field('Name').disabled).toBe(false)
    expect(field('Replicas').value).toBe('2')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalledTimes(1))
    expect(api.createPreset).toHaveBeenLastCalledWith({ name: 'fast copy', spec })
    expect(api.updatePreset).not.toHaveBeenCalled()
    cleanup()
    const long = `${'A'.repeat(34)} tail`
    wrap(<PresetForm open copyOf={preset({ name: long })} {...noop} />)
    await ready()
    expect(field('Name').value).toBe(`${'A'.repeat(34)} copy`)
    cleanup()
    const full = 'B'.repeat(40)
    wrap(<PresetForm open copyOf={preset({ name: full })} {...noop} />)
    await ready()
    expect(field('Name').value).toBe(`${'B'.repeat(35)} copy`)
    expect(field('Name').value.length).toBeLessThanOrEqual(40)
  })

  test('name_rules_default_name_and_pvc', async () => {
    wrap(<PresetForm open {...noop} />)
    await ready()
    await userEvent.type(field('Name'), ' x')
    expect(screen.getByText(/start or end with a space/)).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await replace('Name', 'x ')
    expect(screen.getByText(/start or end with a space/)).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await replace('Name', 'bad/name')
    expect(screen.getByText(/letters, digits, spaces, dots, dashes and underscores/)).toBeTruthy()
    await replace('Name', 'a'.repeat(41))
    expect(screen.getByText(/at most 40/)).toBeTruthy()
    await replace('Name', 'ok name_1.2-3')
    expect(button('Create').disabled).toBe(false)
    await userEvent.clear(field('PVC size'))
    expect(button('Create').disabled).toBe(false)
    cleanup()

    wrap(<PresetForm open preset={preset({ name: 'default', is_default: true })} {...noop} />)
    await ready()
    expect(field('Name').disabled).toBe(true)
    expect(field('PVC size').disabled).toBe(false)
    await replace('PVC size', '30Gi')
    expect(field('PVC size').value).toBe('30Gi')
    cleanup()

    wrap(<PresetForm open preset={preset()} {...noop} />)
    await ready()
    expect(field('Name').disabled).toBe(false)
  })

  test('validates env keys, webhook url and any-mcp json', async () => {
    wrap(<PresetForm open {...noop} />)
    await ready()
    await paste('Name', 'ok')
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
    expect(api.createPreset).not.toHaveBeenCalled()
  })

  test('shows_server_errors', async () => {
    api.createPreset.mockRejectedValueOnce(new ApiError(409, 'conflict', 'preset name fast already exists'))
    wrap(<PresetForm open {...noop} />)
    await ready()
    await userEvent.type(field('Name'), 'fast')
    await userEvent.click(button('Create'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('preset name fast already exists')
    expect(alert.className).toContain('MuiAlert-outlined')
    api.createPreset.mockRejectedValueOnce(new ApiError(422, 'invalid', 'replicas above cap'))
    await userEvent.click(button('Create'))
    expect((await screen.findByRole('alert')).textContent).toBe('replicas above cap')
    expect(screen.queryByText('preset name fast already exists')).toBeNull()
    cleanup()
    api.updatePreset.mockRejectedValueOnce(new ApiError(422, 'invalid', 'model gone not allowed'))
    wrap(<PresetForm open preset={preset()} {...noop} />)
    await ready()
    await userEvent.click(button('Save'))
    expect((await screen.findByRole('alert')).textContent).toBe('model gone not allowed')
  })

  test('cluster_edit_path_retired', () => {
    const tracked = execFileSync('git', ['ls-files', 'src/clusters/ClusterForm.tsx', 'src/clusters/ClusterForm.test.tsx'], { encoding: 'utf8' })
    expect(tracked).toBe('')
    const client = readFileSync('src/api/client.ts', 'utf8')
    expect(client).not.toContain('patchCluster')
    expect(client).not.toContain('PatchCluster')
  })
  test('max replicas renders, validates bounds and maps only when set', { timeout: 15000 }, async () => {
    api.createPreset.mockResolvedValue(preset())
    wrap(<PresetForm open onClose={vi.fn()} onSaved={vi.fn()} />)
    await ready()
    expect(field('Max replicas').value).toBe('')
    expect(screen.getByText('More than 1 makes the cluster a hive')).toBeTruthy()
    await replace('Name', 'a')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalledTimes(1))
    expect(api.createPreset.mock.calls[0][0].spec).not.toHaveProperty('max_replicas')
    await replace('Replicas', '3')
    await replace('Max replicas', '2')
    expect(screen.getByText('Max replicas must be 3 to 16')).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await replace('Max replicas', '17')
    expect(screen.getByText('Max replicas must be 3 to 16')).toBeTruthy()
    await replace('Max replicas', '4')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalledTimes(2))
    expect(api.createPreset.mock.calls[1][0].spec.max_replicas).toBe(4)
  })

  test('static switch maps to spec.static and errors when max replicas differs from replicas', { timeout: 15000 }, async () => {
    api.createPreset.mockResolvedValue(preset())
    wrap(<PresetForm open onClose={vi.fn()} onSaved={vi.fn()} />)
    await ready()
    await replace('Name', 'a')
    await replace('Replicas', '2')
    await replace('Max replicas', '4')
    await userEvent.click(screen.getByLabelText('Static (scaling disabled)'))
    expect(screen.getByText('Static needs max replicas equal to replicas')).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await replace('Max replicas', '2')
    expect(screen.queryByText('Static needs max replicas equal to replicas')).toBeNull()
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalled())
    expect(api.createPreset.mock.calls[0][0].spec.static).toBe(true)
  })

  test('init scripts add, edit and remove rows and submit init_scripts', { timeout: 20000 }, async () => {
    api.createPreset.mockResolvedValue(preset())
    wrap(<PresetForm open {...noop} />)
    await ready()
    expect(screen.getByText('Runs once when a cluster starts, in the shared workdir; agents can read and edit it; edits apply to the next cluster.')).toBeTruthy()
    await replace('Name', 'a')
    await userEvent.click(button('Add init script'))
    await userEvent.click(button('Add init script'))
    await paste('Init script 1', 'echo one')
    await paste('Init script 2', 'echo two')
    await userEvent.click(button('Remove init script 1'))
    expect(field('Init script 1').value).toBe('echo two')
    await userEvent.click(button('Create'))
    await waitFor(() => expect(api.createPreset).toHaveBeenCalledTimes(1))
    expect(api.createPreset.mock.calls[0][0].spec.init_scripts).toEqual(['echo two'])
  })

  test('init scripts sparse: loads stored rows, omits the key when unset, sends [] when all removed', { timeout: 20000 }, async () => {
    api.updatePreset.mockResolvedValue(preset())
    wrap(<PresetForm open preset={preset({ spec: { init_scripts: ['a', 'b'] } })} {...noop} />)
    await ready()
    expect(field('Init script 1').value).toBe('a')
    expect(field('Init script 2').value).toBe('b')
    await userEvent.click(button('Remove init script 2'))
    await userEvent.click(button('Remove init script 1'))
    await userEvent.click(button('Save'))
    await waitFor(() => expect(api.updatePreset).toHaveBeenCalledTimes(1))
    expect(api.updatePreset.mock.calls[0][1].spec).toEqual({ init_scripts: [] })
    cleanup()
    wrap(<PresetForm open preset={preset({ spec: { replicas: 2 } })} {...noop} />)
    await ready()
    await userEvent.click(button('Save'))
    await waitFor(() => expect(api.updatePreset).toHaveBeenCalledTimes(2))
    expect(api.updatePreset.mock.calls[1][1].spec).not.toHaveProperty('init_scripts')
  })

  test('init scripts validation names the script and blocks Save', { timeout: 30000 }, async () => {
    wrap(<PresetForm open {...noop} />)
    await ready()
    await replace('Name', 'a')
    await userEvent.click(button('Add init script'))
    expect(screen.getByText('Init script 1 must not be empty')).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await paste('Init script 1', 'x'.repeat(16385))
    expect(screen.getByText('Init script 1 must be at most 16384 characters')).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
    await replace('Init script 1', 'ok')
    expect(button('Create').disabled).toBe(false)
    for (let i = 0; i < 20; i++) await userEvent.click(button('Add init script'))
    expect(screen.getByText('Init script 21: at most 20 init scripts')).toBeTruthy()
    expect(button('Create').disabled).toBe(true)
  })
})
