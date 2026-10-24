// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { PresetView } from '../api/client'
import { theme } from '../theme'
import { PresetsPage } from './PresetsPage'

const api = vi.hoisted(() => ({ listPresets: vi.fn(), deletePreset: vi.fn() }))
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), ...api }))

/** The form is covered by its own tests: a stub that shows the mode it was opened in and can save. */
vi.mock('./PresetForm', () => ({
  PresetForm: (p: { open: boolean; preset?: PresetView; copyOf?: PresetView; onClose: () => void; onSaved: (p: PresetView) => void }) =>
    p.open ? (
      <div role="dialog" aria-label="Preset form">
        <span>{p.preset ? `edit ${p.preset.name}` : p.copyOf ? `copy ${p.copyOf.name}` : 'new'}</span>
        <button onClick={() => p.onSaved(p.preset ?? p.copyOf ?? ({} as PresetView))}>Save</button>
        <button onClick={p.onClose}>Close</button>
      </div>
    ) : null,
}))

const preset = (id: string, name: string, is_default = false): PresetView =>
  ({ created_at: '2026-01-01T00:00:00Z', id, is_default, name, spec: {}, updated_at: '2026-01-01T00:00:00Z' }) as PresetView
// The server may return them in any order; the page puts the default first.
const PRESETS = [preset('p-big', 'Big box'), preset('p-default', 'Default', true), preset('p-fast', 'Fast')]

const wrap = (onBack = vi.fn()) => ({ onBack, ...render(<ThemeProvider theme={theme}><PresetsPage onBack={onBack} /></ThemeProvider>) })
const rows = () => screen.getAllByRole('listitem')
const row = (name: string) => rows().find((r) => within(r).queryByRole('button', { name: `Edit ${name}` }))!

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  api.listPresets.mockResolvedValue(PRESETS)
  api.deletePreset.mockResolvedValue(undefined)
})
afterEach(cleanup)

describe('PresetsPage', () => {
  test('lists_presets_default_marked_without_delete', async () => {
    wrap()
    expect(screen.getByRole('heading', { name: 'Presets' })).toBeTruthy()
    await screen.findByText('Big box')
    expect(rows().map((r) => r.querySelector('.MuiListItemText-primary')?.textContent)).toEqual(['Default', 'Big box', 'Fast'])
    const def = row('Default')
    expect(within(def).getByText('Default', { selector: '.MuiChip-label' })).toBeTruthy()
    expect(within(def).getByRole('button', { name: 'Edit Default' })).toBeTruthy()
    expect(within(def).getByRole('button', { name: 'Copy Default' })).toBeTruthy()
    expect(within(def).queryByRole('button', { name: /Delete/ })).toBeNull()
    expect(within(row('Big box')).getByRole('button', { name: 'Delete Big box' })).toBeTruthy()
    expect(within(row('Big box')).queryByText('Default', { selector: '.MuiChip-label' })).toBeNull()
  })

  test('new_edit_copy_open_form_and_reload', async () => {
    wrap()
    await screen.findByText('Big box')
    expect(api.listPresets).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('button', { name: 'New preset' }))
    expect(within(screen.getByRole('dialog')).getByText('new')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.listPresets).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('dialog')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Edit Big box' }))
    expect(within(screen.getByRole('dialog')).getByText('edit Big box')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.listPresets).toHaveBeenCalledTimes(3))

    await userEvent.click(screen.getByRole('button', { name: 'Copy Default' }))
    expect(within(screen.getByRole('dialog')).getByText('copy Default')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.listPresets).toHaveBeenCalledTimes(4))
    expect(screen.queryByRole('dialog')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Edit Fast' }))
    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(api.listPresets).toHaveBeenCalledTimes(4)
  })

  test('delete_confirms_and_shows_conflict', async () => {
    wrap()
    await screen.findByText('Big box')
    await userEvent.click(screen.getByRole('button', { name: 'Delete Big box' }))
    expect(screen.getByRole('heading', { name: /Delete .*Big box/ })).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(api.deletePreset).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    await userEvent.click(screen.getByRole('button', { name: 'Delete Big box' }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(api.deletePreset).toHaveBeenCalledWith('p-big'))
    await waitFor(() => expect(api.listPresets).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    api.deletePreset.mockRejectedValue(new ApiError(409, 'last_preset', 'the last preset cannot be deleted'))
    await userEvent.click(screen.getByRole('button', { name: 'Delete Fast' }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('the last preset cannot be deleted')
    expect(alert.className).toContain('MuiAlert-outlined')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  test('back_calls_on_back', async () => {
    const { onBack } = wrap()
    await screen.findByText('Big box')
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onBack).toHaveBeenCalledTimes(1)
  })

  test('list_error_is_shown', async () => {
    api.listPresets.mockRejectedValue(new Error('boom'))
    wrap()
    expect((await screen.findByRole('alert')).textContent).toContain('boom')
  })
})
