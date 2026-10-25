// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { ApiError, type ClusterView } from '../api/client'
import { theme } from '../theme'
import { ScaleControl } from './ScaleControl'

const scaleCluster = vi.hoisted(() => vi.fn())
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), scaleCluster }))

afterEach(() => {
  scaleCluster.mockReset()
  cleanup()
})

const view = (replicas: number, max: number, isStatic = false) =>
  ({ id: 'c1', max_replicas: max, static: isStatic, spec: { replicas, max_replicas: max, static: isStatic } }) as unknown as ClusterView

const mount = (c: ClusterView) =>
  render(
    <ThemeProvider theme={theme}>
      <ScaleControl cluster={c} />
    </ThemeProvider>,
  )

const add = () => screen.getByRole('button', { name: 'Add an agent' })
const remove = () => screen.getByRole('button', { name: 'Remove an agent' })

describe('ScaleControl', () => {
  test('posts scale one up or down', async () => {
    scaleCluster.mockResolvedValue({})
    mount(view(2, 4))
    expect(screen.getByText('2 of 4 agents')).toBeTruthy()
    await act(async () => fireEvent.click(add()))
    expect(scaleCluster).toHaveBeenLastCalledWith('c1', { replicas: 3 })
    await act(async () => fireEvent.click(remove()))
    expect(scaleCluster).toHaveBeenLastCalledWith('c1', { replicas: 1 })
  })

  test('buttons disabled at bounds and in flight', async () => {
    const { unmount } = mount(view(1, 3))
    expect((remove() as HTMLButtonElement).disabled).toBe(true)
    expect((add() as HTMLButtonElement).disabled).toBe(false)
    unmount()
    const m = mount(view(3, 3))
    expect((add() as HTMLButtonElement).disabled).toBe(true)
    m.unmount()
    let done!: () => void
    scaleCluster.mockReturnValue(new Promise<void>((r) => (done = r)))
    mount(view(2, 3))
    await act(async () => fireEvent.click(add()))
    expect((add() as HTMLButtonElement).disabled).toBe(true)
    expect((remove() as HTMLButtonElement).disabled).toBe(true)
    await act(async () => done())
    expect((add() as HTMLButtonElement).disabled).toBe(false)
  })

  test('static and alone clusters', async () => {
    const { container, unmount } = mount(view(1, 1))
    expect(container.textContent).toBe('')
    unmount()
    mount(view(2, 4, true))
    expect(screen.getByText('2 of 4 agents')).toBeTruthy()
    expect((add() as HTMLButtonElement).disabled).toBe(true)
    expect((remove() as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByLabelText('Static cluster: scaling is off')).toBeTruthy()
  })

  test('shows errors inline', async () => {
    scaleCluster.mockRejectedValueOnce(new ApiError(409, 'busy', 'cluster is busy'))
    mount(view(2, 4))
    await act(async () => fireEvent.click(add()))
    expect(screen.getByRole('alert').textContent).toBe('cluster is busy')
    scaleCluster.mockRejectedValueOnce(new ApiError(409, 'leader_would_leave', 'leader is agent 2'))
    await act(async () => fireEvent.click(remove()))
    expect(screen.getByRole('alert').textContent).toBe('leader is agent 2 Make another agent leader first.')
  })
})
