// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { theme } from '../theme'
import { DEFAULT_SIZES, maximize, minimize, parseLayout, reset, resize, restore, restoreMaximized, serialiseLayout, startLayout } from './panelLayout'
import { Workspace } from './Workspace'

const unmounted = vi.hoisted(() => vi.fn())

function setViewport(width: number) {
  window.matchMedia = ((query: string) => {
    const min = /min-width:\s*([\d.]+)px/.exec(query)
    const max = /max-width:\s*([\d.]+)px/.exec(query)
    const matches = (!min || width >= Number(min[1])) && (!max || width <= Number(max[1]))
    return { matches, media: query, onchange: null, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }
  }) as typeof window.matchMedia
}

/** jsdom has no layout: give every element a 1000 x 500 box so react-resizable-panels computes real sizes. */
function stubGeometry() {
  class NoopObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', NoopObserver)
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(1000)
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(500)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 1000, height: 500, top: 0, left: 0, right: 1000, bottom: 500, x: 0, y: 0, toJSON: () => ({}) })
}

function Slot({ name }: { name: string }) {
  useEffect(() => () => unmounted(name), [name])
  return <div data-testid={`slot-${name}`}>{name} content</div>
}

function mount(clusterId = 'c1') {
  return render(
    <ThemeProvider theme={theme}>
      <Workspace clusterId={clusterId} files={<Slot name="files" />} chat={<Slot name="chat" />} terminal={<Slot name="terminal" />} />
    </ThemeProvider>,
  )
}

const grow = (id: string) => Number(screen.getByTestId(id).style.flexGrow)
const stored = (id = 'c1') => JSON.parse(localStorage.getItem(`orch:layout:${id}`) ?? 'null')
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }))
const strip = (name: string) => screen.queryByTestId(`strip-${name}`)

beforeEach(() => {
  setViewport(1400)
  stubGeometry()
  localStorage.clear()
  unmounted.mockClear()
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('panelLayout', () => {
  test('defaults and the rules', () => {
    const l = startLayout()
    expect(l.sizes).toEqual([25, 45, 30])
    expect(DEFAULT_SIZES).toEqual([25, 45, 30])
    expect(l.collapsed.size).toBe(0)
    const m = minimize(minimize(l, 'files'), 'chat')
    expect([...m.collapsed]).toEqual(['files', 'chat'])
    expect(minimize(m, 'terminal')).toBe(m)
    const x = maximize(l, 'chat')
    expect([...x.collapsed].sort()).toEqual(['files', 'terminal'])
    expect(restoreMaximized(x)).toEqual(l)
    expect(restore(x, 'files').collapsed.has('files')).toBe(false)
    expect(resize(l, [10, 10, 80]).sizes).toEqual([10, 10, 80])
    expect(reset()).toEqual(l)
  })

  test('serialise and parse round trip; invalid data falls back', () => {
    const l = minimize(resize(startLayout(), [20, 50, 30]), 'files')
    expect(parseLayout(JSON.parse(JSON.stringify(serialiseLayout(l))))).toEqual(l)
    for (const bad of [null, 7, 'x', {}, { sizes: [1, 2], collapsed: [] }, { sizes: [25, 45, 'a'], collapsed: [] }, { sizes: [25, 45, 30], collapsed: ['files', 'chat', 'terminal'] }, { sizes: [25, 45, 30], collapsed: ['nope'] }, { sizes: [-1, 90, 11], collapsed: [] }]) {
      expect(parseLayout(bad)).toEqual(startLayout())
    }
  })
})

describe('Workspace', () => {
  test('renders three panels with headers and two separators', () => {
    mount()
    expect(screen.getAllByRole('separator')).toHaveLength(2)
    for (const name of ['Files', 'Chat', 'Terminal']) {
      expect(screen.getByRole('button', { name: `Minimize ${name}` })).toBeTruthy()
      expect(screen.getByRole('button', { name: `Maximize ${name}` })).toBeTruthy()
    }
    expect(screen.getByTestId('slot-files')).toBeTruthy()
    expect(grow('files')).toBe(25)
    expect(grow('chat')).toBe(45)
    expect(grow('terminal')).toBe(30)
  })

  test('at_most_two_minimized', () => {
    mount()
    click('Minimize Files')
    click('Minimize Chat')
    expect(strip('files')).toBeTruthy()
    expect(strip('chat')).toBeTruthy()
    const last = screen.getByRole('button', { name: 'Minimize Terminal' }) as HTMLButtonElement
    expect(last.disabled).toBe(true)
    fireEvent.click(last)
    expect(strip('terminal')).toBeNull()
    expect(stored().collapsed.sort()).toEqual(['chat', 'files'])
  })

  test('maximize_and_restore', () => {
    mount()
    click('Maximize Chat')
    expect(strip('files')).toBeTruthy()
    expect(strip('terminal')).toBeTruthy()
    expect(strip('chat')).toBeNull()
    expect(stored().collapsed.sort()).toEqual(['files', 'terminal'])
    click('Restore Files')
    expect(strip('files')).toBeNull()
    expect(strip('terminal')).toBeTruthy()
    click('Restore Terminal')
    expect(strip('terminal')).toBeNull()
    expect(stored().collapsed).toEqual([])
  })

  test('minimized_strip_has_name_and_restore', () => {
    mount()
    click('Minimize Files')
    const s = strip('files')!
    expect(within(s).getByText('Files')).toBeTruthy()
    expect(within(s).getByRole('button', { name: 'Restore Files' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Minimize Files' })).toBeNull()
    expect(screen.getByTestId('files').getAttribute('data-collapsed')).toBe('true')
    fireEvent.click(within(s).getByRole('button', { name: 'Restore Files' }))
    expect(strip('files')).toBeNull()
    expect(screen.getByRole('button', { name: 'Minimize Files' })).toBeTruthy()
  })

  test('separator_double_click_resets', () => {
    localStorage.setItem('orch:layout:c1', JSON.stringify({ sizes: [10, 60, 30], collapsed: ['terminal'] }))
    mount()
    expect(strip('terminal')).toBeTruthy()
    fireEvent.doubleClick(screen.getAllByRole('separator')[0])
    expect(strip('terminal')).toBeNull()
    expect([grow('files'), grow('chat'), grow('terminal')]).toEqual([25, 45, 30])
    expect(stored()).toEqual({ sizes: [25, 45, 30], collapsed: [], before: null })
  })

  test('layout_persists_per_cluster', () => {
    const first = mount('c1')
    click('Minimize Terminal')
    expect(stored('c1').collapsed).toEqual(['terminal'])
    expect(localStorage.getItem('orch:layout:c2')).toBeNull()
    first.unmount()
    mount('c1')
    expect(strip('terminal')).toBeTruthy()
    cleanup()
    mount('c2')
    expect(strip('terminal')).toBeNull()
    expect(stored('c2').collapsed).toEqual([])
    cleanup()
    localStorage.setItem('orch:layout:c3', '{"sizes":"junk"')
    mount('c3')
    expect([grow('files'), grow('chat'), grow('terminal')]).toEqual([25, 45, 30])
    cleanup()
    localStorage.setItem('orch:layout:c4', JSON.stringify({ sizes: [1, 2], collapsed: ['files', 'chat', 'terminal'] }))
    mount('c4')
    expect(strip('files')).toBeNull()
    expect([grow('files'), grow('chat'), grow('terminal')]).toEqual([25, 45, 30])
  })

  test('phone_three_way_switch', () => {
    setViewport(400)
    mount()
    expect(screen.queryAllByRole('separator')).toHaveLength(0)
    const group = screen.getByRole('group', { name: 'Panel' })
    const buttons = within(group).getAllByRole('button')
    expect(buttons.map((b) => b.textContent)).toEqual(['Files', 'Chat', 'Terminal'])
    expect(screen.getByRole('button', { name: 'Chat' }).getAttribute('aria-pressed')).toBe('true')
    const visible = () => ['files', 'chat', 'terminal'].filter((n) => screen.getByTestId(`slot-${n}`).closest('[hidden]') === null)
    expect(visible()).toEqual(['chat'])
    fireEvent.click(screen.getByRole('button', { name: 'Terminal' }))
    expect(visible()).toEqual(['terminal'])
    fireEvent.click(screen.getByRole('button', { name: 'Files' }))
    expect(visible()).toEqual(['files'])
    fireEvent.click(screen.getByRole('button', { name: 'Files' }))
    expect(visible()).toEqual(['files'])
  })

  test('maximized_restore_returns_previous_layout', () => {
    localStorage.setItem('orch:layout:c1', JSON.stringify({ sizes: [20, 50, 30], collapsed: ['terminal'] }))
    mount()
    const before = [grow('files'), grow('chat')]
    expect(before[0]).toBe(20)
    click('Maximize Chat')
    expect(strip('files')).toBeTruthy()
    expect(strip('terminal')).toBeTruthy()
    const chat = screen.getByTestId('chat')
    expect(within(chat).getAllByRole('button', { name: /^Restore/ })).toHaveLength(1)
    expect(within(chat).queryByRole('button', { name: 'Maximize Chat' })).toBeNull()
    fireEvent.click(within(chat).getByRole('button', { name: 'Restore Chat' }))
    expect(strip('files')).toBeNull()
    expect(strip('terminal')).toBeTruthy()
    expect(stored()).toEqual({ sizes: [20, 50, 30], collapsed: ['terminal'], before: null })
    expect([grow('files'), grow('chat')]).toEqual(before)
  })

  test('restore_minimized_to_previous_size', () => {
    localStorage.setItem('orch:layout:c1', JSON.stringify({ sizes: [20, 50, 30], collapsed: [] }))
    mount()
    expect([grow('files'), grow('chat'), grow('terminal')]).toEqual([20, 50, 30])
    click('Minimize Files')
    expect(grow('files')).toBeLessThan(5)
    click('Restore Files')
    expect([grow('files'), grow('chat'), grow('terminal')]).toEqual([20, 50, 30])
    expect(stored().sizes).toEqual([20, 50, 30])
  })

  test('minimized_panel_stays_mounted', () => {
    mount()
    click('Minimize Terminal')
    click('Minimize Files')
    for (const name of ['files', 'terminal']) {
      const slot = screen.getByTestId(`slot-${name}`)
      expect(slot.isConnected).toBe(true)
      expect(slot.closest('[hidden]')).not.toBeNull()
    }
    expect(unmounted).not.toHaveBeenCalled()
    click('Restore Files')
    expect(screen.getByTestId('slot-files').closest('[hidden]')).toBeNull()
    expect(unmounted).not.toHaveBeenCalled()
  })

  test('keyboard_resize_adopts_and_persists_sizes', () => {
    mount()
    const separator = screen.getAllByRole('separator')[0]
    fireEvent.keyDown(separator, { key: 'ArrowRight' })
    expect([grow('files'), grow('chat'), grow('terminal')]).toEqual([30, 40, 30])
    expect(stored().sizes).toEqual([30, 40, 30])
    expect(stored().collapsed).toEqual([])
  })

  test('keyboard_resize_survives_remount', () => {
    const first = mount()
    fireEvent.keyDown(screen.getAllByRole('separator')[0], { key: 'ArrowRight' })
    first.unmount()
    mount()
    expect([grow('files'), grow('chat'), grow('terminal')]).toEqual([30, 40, 30])
  })

  test('drag_collapse_is_adopted_and_persisted', () => {
    mount()
    fireEvent.keyDown(screen.getAllByRole('separator')[0], { key: 'Home' })
    expect(strip('files')).toBeTruthy()
    expect(stored().collapsed).toEqual(['files'])
  })

  test('unmounting the workspace unmounts its slots', () => {
    const view = mount()
    act(() => view.unmount())
    expect(unmounted).toHaveBeenCalledTimes(3)
  })
})
