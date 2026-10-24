// @vitest-environment jsdom
import { ThemeProvider } from '@mui/material/styles'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { FileContent, FileListing } from '../api/files'
import type { StreamOptions } from '../api/stream'
import { theme } from '../theme'
import { FilesPanel } from './FilesPanel'
import { useFileTree } from './useFileTree'

const api = vi.hoisted(() => ({ listFiles: vi.fn(), readFile: vi.fn() }))
const stream = vi.hoisted(() => ({ opened: [] as { url: string; opts: StreamOptions }[], close: vi.fn() }))

vi.mock('../api/files', () => api)
vi.mock('../api/stream', () => ({
  openStream: (url: string, opts: StreamOptions) => {
    stream.opened.push({ url, opts })
    return { close: stream.close }
  },
}))

type Entry = FileListing['entries'][number]
const entry = (name: string, type: Entry['type'] = 'file'): Entry => ({ name, type, size: type === 'file' ? 10 : null, mtime: '2026-10-23T00:00:00Z' })
const listing = (path: string, entries: Entry[], next_offset: number | null = null): FileListing => ({
  agent_id: 'a1',
  cluster_id: 'c1',
  entries,
  next_offset,
  path,
  root: '/work',
})
const content = (path: string, over: Partial<FileContent>): FileContent => ({
  agent_id: 'a1',
  cluster_id: 'c1',
  content: null,
  kind: 'text',
  mime: null,
  mtime: '2026-10-23T00:00:00Z',
  path,
  size: 10,
  ...over,
})

/** Directory listings by relative path; a function value is called per request. */
function serve(dirs: Record<string, FileListing | (() => FileListing)>) {
  api.listFiles.mockImplementation(async (_c: string, path: string) => {
    const d = dirs[path]
    if (!d) throw new ApiError(404, 'not_found', 'no such directory')
    return typeof d === 'function' ? d() : d
  })
}

const mount = (clusterId = 'c1') =>
  render(
    <ThemeProvider theme={theme}>
      <FilesPanel clusterId={clusterId} />
    </ThemeProvider>,
  )

/** A cluster event as the SSE stream delivers it. */
const emit = (type: string, data: Record<string, unknown>) =>
  act(async () => {
    stream.opened[stream.opened.length - 1].opts.onEvent({ id: 1, type, data }, { type, id: '1' })
  })

const calls = () => api.listFiles.mock.calls.map((c) => c.slice(0, 3))

beforeEach(() => {
  api.listFiles.mockReset()
  api.readFile.mockReset()
  stream.opened.length = 0
  stream.close.mockReset()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('FilesPanel', () => {
  test('lazy_load_on_expand', async () => {
    serve({
      '': listing('', [entry('src', 'dir'), entry('.git', 'dir'), entry('.env'), entry('README.md')]),
      src: listing('src', [entry('main.py')]),
    })
    mount()
    expect(await screen.findByText('README.md')).toBeTruthy()
    // Everything is listed, dotfiles and .git included; only the root has been fetched.
    expect(screen.getByText('.git')).toBeTruthy()
    expect(screen.getByText('.env')).toBeTruthy()
    expect(calls()).toEqual([['c1', '']])
    expect(screen.queryByText('main.py')).toBeNull()

    fireEvent.click(screen.getByText('src'))
    expect(await screen.findByText('main.py')).toBeTruthy()
    expect(calls()).toEqual([['c1', ''], ['c1', 'src']])
    // The sibling folder was never expanded.
    expect(calls().some((c) => c[1] === '.git')).toBe(false)
  })

  test('more_row_pages', async () => {
    api.listFiles.mockImplementation(async (_c: string, _p: string, offset?: number) =>
      offset ? listing('', [entry('c.txt'), entry('d.txt')]) : listing('', [entry('a.txt'), entry('b.txt')], 2),
    )
    mount()
    const more = await screen.findByText('Load more')
    expect(screen.queryByText('c.txt')).toBeNull()
    fireEvent.click(more)
    expect(await screen.findByText('d.txt')).toBeTruthy()
    expect(calls()[1]).toEqual(['c1', '', 2])
    // The first page stays, the page is appended, and the more row is gone on the last page.
    expect(screen.getByText('a.txt')).toBeTruthy()
    expect(screen.getByText('c.txt')).toBeTruthy()
    expect(screen.queryByText('Load more')).toBeNull()
  })

  test('preview_kinds', async () => {
    serve({ '': listing('', [entry('a.txt'), entry('logo.png'), entry('huge.log')]) })
    api.readFile.mockImplementation(async (_c: string, path: string) => {
      if (path === 'a.txt') return content(path, { kind: 'text', content: 'hello world\nline two', size: 20, mime: 'text/plain' })
      if (path === 'logo.png') return content(path, { kind: 'binary', size: 2048, mime: 'image/png' })
      return content(path, { kind: 'too_large', size: 99999999 })
    })
    mount()
    fireEvent.click(await screen.findByText('a.txt'))
    const pre = await screen.findByTestId('file-preview-text')
    expect(pre.tagName).toBe('PRE')
    expect(pre.textContent).toBe('hello world\nline two')
    expect(api.readFile).toHaveBeenLastCalledWith('c1', 'a.txt')

    fireEvent.click(screen.getByText('logo.png'))
    const binary = await screen.findByTestId('file-preview-note')
    expect(binary.textContent).toContain('2048')
    expect(binary.textContent).toContain('image/png')
    expect(screen.queryByTestId('file-preview-text')).toBeNull()

    fireEvent.click(screen.getByText('huge.log'))
    await waitFor(() => expect(screen.getByTestId('file-preview-note').textContent).toContain('too large to preview'))
    expect(screen.getByTestId('file-preview-note').textContent).toContain('99999999')
  })

  describe('event_driven_refresh', () => {
    const tree = () => {
      let n = 0
      serve({
        '': () => listing('', n++ ? [entry('src', 'dir'), entry('root_new.txt')] : [entry('src', 'dir'), entry('lib', 'dir')]),
        src: listing('src', [entry('main.py')]),
        lib: listing('lib', [entry('x.py')]),
      })
    }

    test('event_driven_refresh opens a live-tail stream for tool_call and tool_result', async () => {
      tree()
      mount()
      await screen.findByText('src')
      expect(stream.opened).toHaveLength(1)
      expect(stream.opened[0].url).toBe('/api/v1/clusters/c1/events/stream')
      expect([...(stream.opened[0].opts.eventNames ?? [])].sort()).toEqual(['tool_call', 'tool_result'])
      expect(stream.opened[0].opts.afterId).toBeUndefined()
    })

    /** A write tool's call followed by its result, as the stream delivers them (the call fires before the tool runs). */
    let seq = 0
    const call = async (name: string, input: Record<string, unknown>) => {
      const id = `u${++seq}`
      await emit('tool_call', { tool_use_id: id, name, tool_input: input })
      return id
    }
    const result = (id: string) => emit('tool_result', { tool_use_id: id, response: 'ok', is_error: false })

    test('event_driven_refresh Write and Edit refresh the loaded parent directory on the result only', async () => {
      let version = 0
      serve({
        '': listing('', [entry('src', 'dir'), entry('lib', 'dir')]),
        src: () => listing('src', version++ ? [entry('main.py'), entry('new.py')] : [entry('main.py')]),
        lib: listing('lib', [entry('x.py')]),
      })
      mount()
      fireEvent.click(await screen.findByText('src'))
      await screen.findByText('main.py')
      const before = calls().length

      const id = await call('Write', { file_path: '/work/src/new.py' })
      // The call fires before the tool runs: nothing is refreshed yet.
      expect(calls().length).toBe(before)
      await result(id)
      expect(await screen.findByText('new.py')).toBeTruthy()
      expect(calls().slice(before)).toEqual([['c1', 'src']])

      // Outside the root, wrong tool, a missing path and an unknown result id are all ignored.
      await result(await call('Write', { file_path: '/elsewhere/src/z.py' }))
      await result(await call('Write', { file_path: '/workshop/src/z.py' }))
      await result(await call('Read', { file_path: '/work/src/main.py' }))
      await result(await call('Edit', {}))
      await result('never-called')
      expect(calls().length).toBe(before + 1)

      // Edit and NotebookEdit work, also through the alternate accessors; a file in the root refreshes the root.
      const alt = `u${++seq}`
      await emit('tool_call', { tool_use_id: alt, tool_name: 'Edit', input: { file_path: '/work/root.txt' } })
      await result(alt)
      await result(await call('NotebookEdit', { file_path: '/work/src/n.ipynb' }))
      expect(calls().slice(before + 1)).toEqual([['c1', ''], ['c1', 'src']])
    })

    test('event_driven_refresh a Write into a new directory refreshes the nearest loaded ancestor so the new directory appears', async () => {
      let version = 0
      serve({ '': () => listing('', version++ ? [entry('src', 'dir'), entry('fresh', 'dir')] : [entry('src', 'dir')]), src: listing('src', []) })
      mount()
      await screen.findByText('src')
      const before = calls().length
      await result(await call('Write', { file_path: '/work/fresh/deep/a.py' }))
      expect(await screen.findByText('fresh')).toBeTruthy()
      expect(calls().slice(before)).toEqual([['c1', '']])
    })

    test('event_driven_refresh a NotebookEdit with notebook_path refreshes its parent directory', async () => {
      let version = 0
      serve({ '': listing('', [entry('src', 'dir')]), src: () => listing('src', version++ ? [entry('a.ipynb'), entry('b.ipynb')] : [entry('a.ipynb')]) })
      mount()
      fireEvent.click(await screen.findByText('src'))
      await screen.findByText('a.ipynb')
      const before = calls().length
      await result(await call('NotebookEdit', { notebook_path: '/work/src/a.ipynb' }))
      expect(await screen.findByText('b.ipynb')).toBeTruthy()
      expect(calls().slice(before)).toEqual([['c1', 'src']])
    })

    test('event_driven_refresh a written file open in the preview is refetched on the result', async () => {
      serve({ '': listing('', [entry('a.txt'), entry('b.txt')]) })
      let version = 0
      api.readFile.mockImplementation(async (_c: string, path: string) => content(path, { content: `v${version++}` }))
      mount()
      fireEvent.click(await screen.findByText('a.txt'))
      await screen.findByText('v0')
      const id = await call('Edit', { file_path: '/work/a.txt' })
      expect(api.readFile).toHaveBeenCalledTimes(1)
      await result(id)
      expect(await screen.findByText('v1')).toBeTruthy()
      // Another file is not the open one.
      await result(await call('Write', { file_path: '/work/b.txt' }))
      expect(api.readFile).toHaveBeenCalledTimes(2)
    })

    test('event_driven_refresh Bash results refresh expanded directories at most once a second', async () => {
      tree()
      mount()
      fireEvent.click(await screen.findByText('src'))
      await screen.findByText('main.py')
      const before = calls().length
      vi.useFakeTimers()

      await emit('tool_result', { tool_name: 'Bash' })
      await act(async () => {})
      // The root and the expanded src are refreshed; the never-expanded lib is not.
      expect(calls().slice(before).map((c) => c[1]).sort()).toEqual(['', 'src'])

      await emit('tool_result', { tool_name: 'Bash' })
      await emit('tool_result', { tool_name: 'Bash' })
      await emit('tool_result', { tool_name: 'Read' })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(900)
      })
      expect(calls().length).toBe(before + 2)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200)
      })
      // The burst collapsed into one trailing pass.
      expect(calls().length).toBe(before + 4)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000)
      })
      expect(calls().length).toBe(before + 4)
    })
  })

  test('manual_refresh_and_errors', async () => {
    let up = false
    api.listFiles.mockImplementation(async (_c: string, path: string) => {
      if (!up) throw new ApiError(409, 'agent_not_ready', 'no agent of the cluster is ready')
      return path === '' ? listing('', [entry('src', 'dir'), entry('fresh.txt')]) : listing(path, [entry('main.py')])
    })
    mount()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('agent_not_ready')
    expect(screen.queryByText('workdir not created yet')).toBeNull()

    up = true
    fireEvent.click(screen.getByRole('button', { name: 'Refresh files' }))
    expect(await screen.findByText('fresh.txt')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()

    // The refresh button re-fetches the root and every expanded directory.
    fireEvent.click(screen.getByText('src'))
    await screen.findByText('main.py')
    api.listFiles.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh files' }))
    await waitFor(() => expect(calls().map((c) => c[1]).sort()).toEqual(['', 'src']))

    // A failing folder shows its error inline and keeps the tree.
    up = false
    fireEvent.click(screen.getByRole('button', { name: 'Refresh files' }))
    expect((await screen.findAllByRole('alert'))[0].textContent).toContain('agent_not_ready')
    expect(screen.getByText('fresh.txt')).toBeTruthy()
  })

  test('symlink_previews_and_escape_error', async () => {
    serve({ '': listing('', [entry('link.txt', 'symlink'), entry('escape', 'symlink')]) })
    api.readFile.mockImplementation(async (_c: string, path: string) => {
      if (path === 'escape') throw new ApiError(400, 'invalid_path', 'path escapes the workdir')
      return content(path, { kind: 'text', content: 'target text' })
    })
    mount()
    fireEvent.click(await screen.findByText('link.txt'))
    expect((await screen.findByTestId('file-preview-text')).textContent).toBe('target text')
    expect(api.readFile).toHaveBeenCalledWith('c1', 'link.txt')
    // A symlink is a leaf: it is never listed as a directory.
    expect(calls()).toEqual([['c1', '']])

    fireEvent.click(screen.getByText('escape'))
    const err = await screen.findByTestId('file-preview-error')
    expect(err.textContent).toContain('invalid_path')
    expect(err.textContent).toContain('escapes the workdir')
    expect(screen.queryByTestId('file-preview-text')).toBeNull()
  })

  test('other_entries_type_only', async () => {
    serve({ '': listing('', [entry('sock', 'other')]) })
    mount()
    fireEvent.click(await screen.findByText('sock'))
    const note = await screen.findByTestId('file-preview-note')
    expect(note.textContent).toContain('other')
    expect(api.readFile).not.toHaveBeenCalled()
    expect(calls()).toEqual([['c1', '']])
  })

  test('missing_root_empty_tree', async () => {
    api.listFiles.mockRejectedValue(new ApiError(404, 'not_found', 'workdir does not exist'))
    mount()
    expect(await screen.findByText('workdir not created yet')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryAllByRole('treeitem')).toHaveLength(0)
  })

  test('event_stream_closes_on_unmount', async () => {
    serve({ '': listing('', [entry('a.txt')]) })
    const view = mount()
    await screen.findByText('a.txt')
    expect(stream.opened).toHaveLength(1)
    expect(stream.close).not.toHaveBeenCalled()

    // A cluster change swaps the subscription.
    view.rerender(
      <ThemeProvider theme={theme}>
        <FilesPanel clusterId="c2" />
      </ThemeProvider>,
    )
    await waitFor(() => expect(stream.opened).toHaveLength(2))
    expect(stream.opened[1].url).toBe('/api/v1/clusters/c2/events/stream')
    expect(stream.close).toHaveBeenCalledTimes(1)

    view.unmount()
    expect(stream.close).toHaveBeenCalledTimes(2)
  })

  test('preview_dropped_on_cluster_change', async () => {
    serve({ '': listing('', [entry('a.txt')]) })
    api.readFile.mockResolvedValue(content('a.txt', { content: 'one' }))
    const view = mount()
    fireEvent.click(await screen.findByText('a.txt'))
    await screen.findByTestId('file-preview-text')
    view.rerender(
      <ThemeProvider theme={theme}>
        <FilesPanel clusterId="c2" />
      </ThemeProvider>,
    )
    await waitFor(() => expect(screen.queryByTestId('file-preview-text')).toBeNull())
  })
})

describe('useFileTree', () => {
  test('an unchanged listing refresh keeps the tree state object', async () => {
    serve({ '': listing('', [entry('a.txt')]) })
    const { result } = renderHook(() => useFileTree('c1'))
    await waitFor(() => expect(result.current.state.dirs[''] && !result.current.state.dirs[''].loading).toBe(true))
    const before = result.current.state
    let renders = 0
    renderHook(() => void renders++)
    await act(async () => {
      result.current.refresh('')
      await new Promise((r) => setTimeout(r, 10))
    })
    expect(api.listFiles).toHaveBeenCalledTimes(2)
    expect(result.current.state).toBe(before)
    // A changed listing does commit.
    serve({ '': listing('', [entry('a.txt'), entry('b.txt')]) })
    await act(async () => {
      result.current.refresh('')
      await new Promise((r) => setTimeout(r, 10))
    })
    expect(result.current.state).not.toBe(before)
    expect(result.current.state.dirs[''].entries).toHaveLength(2)
  })

  test('an unchanged listing refresh of a multi-page directory causes no state change', async () => {
    api.listFiles.mockImplementation(async (_c: string, _p: string, offset?: number) =>
      offset === undefined ? listing('', [entry('a.txt')], 1) : listing('', [entry('b.txt')]),
    )
    const { result } = renderHook(() => useFileTree('c1'))
    await waitFor(() => expect(result.current.state.dirs['']?.loading).toBe(false))
    act(() => result.current.loadMore(''))
    await waitFor(() => expect(result.current.state.dirs[''].entries).toHaveLength(2))
    const before = result.current.state
    await act(async () => {
      result.current.refresh('')
      await new Promise((r) => setTimeout(r, 10))
    })
    expect(api.listFiles).toHaveBeenCalledTimes(4)
    expect(result.current.state).toBe(before)
  })

  test('pollExpanded skips a directory whose load is in flight', async () => {
    let release: (l: FileListing) => void = () => {}
    api.listFiles.mockImplementation(async (_c: string, path: string) => {
      if (path === 'src') return new Promise<FileListing>((r) => (release = r))
      return listing('', [entry('src', 'dir')])
    })
    const { result } = renderHook(() => useFileTree('c1'))
    await waitFor(() => expect(result.current.state.dirs['']?.loading).toBe(false))
    act(() => result.current.expand('src'))
    const before = api.listFiles.mock.calls.length
    act(() => result.current.pollExpanded())
    expect(api.listFiles.mock.calls.slice(before).map((c) => c[1])).toEqual([''])
    await act(async () => release(listing('src', [entry('x.py')])))
    await waitFor(() => expect(result.current.state.dirs.src?.entries).toHaveLength(1))
  })

  test('pollExpanded retries a root in error or missing state', async () => {
    let fail: ApiError | null = new ApiError(409, 'agent_not_ready', 'not ready')
    api.listFiles.mockImplementation(async () => {
      if (fail) throw fail
      return listing('', [entry('a.txt')])
    })
    const { result } = renderHook(() => useFileTree('c1'))
    await waitFor(() => expect(result.current.state.dirs['']?.error).toContain('agent_not_ready'))
    fail = new ApiError(404, 'not_found', 'gone')
    await act(async () => result.current.pollExpanded())
    await waitFor(() => expect(result.current.state.missing).toBe(true))
    fail = null
    await act(async () => result.current.pollExpanded())
    await waitFor(() => expect(result.current.state.dirs[''].entries).toHaveLength(1))
    expect(result.current.state.missing).toBe(false)
  })
})
