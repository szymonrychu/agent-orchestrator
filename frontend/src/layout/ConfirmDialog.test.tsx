// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { ConfirmDialog } from './ConfirmDialog'

afterEach(cleanup)

function setup(open = true) {
  const onConfirm = vi.fn()
  const onCancel = vi.fn()
  render(
    <ConfirmDialog open={open} title="Discard cluster?" body="This cannot be undone." confirmLabel="Discard" onConfirm={onConfirm} onCancel={onCancel} />,
  )
  return { onConfirm, onCancel }
}

describe('ConfirmDialog', () => {
  test('confirm calls onConfirm once and never onCancel', async () => {
    const { onConfirm, onCancel } = setup()
    await userEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(onCancel).not.toHaveBeenCalled()
  })

  test('Cancel calls onCancel and never onConfirm', async () => {
    const { onConfirm, onCancel } = setup()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  test('Escape calls onCancel and never onConfirm', async () => {
    const { onConfirm, onCancel } = setup()
    await userEvent.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  test('backdrop click calls onCancel and never onConfirm', async () => {
    const { onConfirm, onCancel } = setup()
    const backdrop = document.querySelector('.MuiBackdrop-root') as HTMLElement
    await userEvent.click(backdrop)
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  test('shows title and body', () => {
    setup()
    expect(screen.getByText('Discard cluster?')).toBeTruthy()
    expect(screen.getByText('This cannot be undone.')).toBeTruthy()
  })

  test('closed renders nothing', () => {
    setup(false)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByText('Discard cluster?')).toBeNull()
  })
})
