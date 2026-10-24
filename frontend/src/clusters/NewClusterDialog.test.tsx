// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { ClusterView, PresetView } from '../api/client'
import { theme } from '../theme'
import { MAX_SLUG } from './clusterSpecForm'
import { NewClusterDialog } from './NewClusterDialog'

const api = vi.hoisted(() => ({ listPresets: vi.fn(), createCluster: vi.fn() }))
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), ...api }))

const preset = (id: string, name: string, is_default = false): PresetView => ({ created_at: '2026-01-01T00:00:00Z', id, is_default, name, spec: {}, updated_at: '2026-01-01T00:00:00Z' })
const PRESETS = [preset('p-big', 'Big box'), preset('p-def', 'Default', true), preset('p-small', 'Small')]
const created = { id: 'c-1', slug: 'default' } as ClusterView

const wrap = (el: React.ReactElement) => render(<ThemeProvider theme={theme}>{el}</ThemeProvider>)
const create = () => screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement
const slug = () => screen.getByLabelText('Slug') as HTMLInputElement
const presetSelect = () => screen.getByRole('combobox', { name: 'Preset' })

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  api.listPresets.mockResolvedValue(PRESETS)
})
afterEach(cleanup)

describe('NewClusterDialog', () => {
  test('preselects_default_and_only_preset_and_slug', async () => {
    wrap(<NewClusterDialog open onClose={vi.fn()} onCreated={vi.fn()} />)
    await waitFor(() => expect(presetSelect().textContent).toBe('Default'))
    expect(api.listPresets).toHaveBeenCalledTimes(1)
    await userEvent.click(presetSelect())
    expect(within(screen.getByRole('listbox')).getAllByRole('option').map((o) => o.textContent)).toEqual(['Big box', 'Default', 'Small'])
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(screen.getByText(/generated from the preset name/i)).toBeTruthy()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getAllByRole('textbox').length + within(dialog).getAllByRole('combobox').length).toBe(2)
    expect(slug().value).toBe('')
    expect(within(dialog).getAllByRole('button').map((b) => b.textContent)).toEqual(['Cancel', 'Create'])
  })

  test('sends_preset_id_and_optional_slug', async () => {
    api.createCluster.mockResolvedValue(created)
    const onCreated = vi.fn()
    wrap(<NewClusterDialog open onClose={vi.fn()} onCreated={onCreated} />)
    await waitFor(() => expect(presetSelect().textContent).toBe('Default'))
    await userEvent.click(create())
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created))
    expect(api.createCluster).toHaveBeenLastCalledWith({ preset_id: 'p-def' })

    await userEvent.click(presetSelect())
    await userEvent.click(screen.getByRole('option', { name: 'Small' }))
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    await userEvent.click(slug())
    await userEvent.paste('my-slug')
    await userEvent.click(create())
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(2))
    expect(api.createCluster).toHaveBeenLastCalledWith({ preset_id: 'p-small', slug: 'my-slug' })
  })

  test('shows_server_error_and_blocks_long_slug', async () => {
    api.createCluster.mockRejectedValue(new ApiError(422, 'invalid_spec', 'preset "Default": replicas exceeds the cap'))
    const onCreated = vi.fn()
    const onClose = vi.fn()
    wrap(<NewClusterDialog open onClose={onClose} onCreated={onCreated} />)
    await waitFor(() => expect(presetSelect().textContent).toBe('Default'))
    await userEvent.click(create())
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('preset "Default": replicas exceeds the cap')
    expect(onCreated).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(create().disabled).toBe(false)

    await userEvent.click(slug())
    await userEvent.paste('a'.repeat(MAX_SLUG + 1))
    expect(create().disabled).toBe(true)
    expect(screen.getByText(`Slug must be at most ${MAX_SLUG} characters`)).toBeTruthy()
    await userEvent.clear(slug())
    await userEvent.paste('a'.repeat(MAX_SLUG))
    expect(create().disabled).toBe(false)
  })

  test('shows_preset_list_load_error', async () => {
    api.listPresets.mockRejectedValue(new ApiError(500, 'boom', 'cannot list presets'))
    wrap(<NewClusterDialog open onClose={vi.fn()} onCreated={vi.fn()} />)
    expect((await screen.findByRole('alert')).textContent).toContain('cannot list presets')
    expect(create().disabled).toBe(true)
  })
})
