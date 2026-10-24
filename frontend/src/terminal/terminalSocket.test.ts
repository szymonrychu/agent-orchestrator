import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { terminalSocketUrl } from '../api/terminals'
import { MAX_FRAME_BYTES, openTerminalSocket, type TerminalStatus } from './terminalSocket'

class FakeWebSocket {
  static all: FakeWebSocket[] = []
  readyState = 0
  binaryType = ''
  sent: (string | Uint8Array)[] = []
  closedWith: number | undefined
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.all.push(this)
  }
  send(data: string | Uint8Array) {
    this.sent.push(data)
  }
  close(code?: number) {
    this.closedWith = code
    this.readyState = 3
  }
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  text(obj: unknown) {
    this.onmessage?.({ data: JSON.stringify(obj) })
  }
  bytes(...b: number[]) {
    this.onmessage?.({ data: new Uint8Array(b).buffer })
  }
  drop(code: number) {
    this.readyState = 3
    this.onclose?.({ code })
  }
}

const impl = FakeWebSocket as unknown as typeof WebSocket
const last = () => FakeWebSocket.all[FakeWebSocket.all.length - 1]
const dec = new TextDecoder()

function setup() {
  const data: Uint8Array[] = []
  const exits: (number | null)[] = []
  const statuses: TerminalStatus[] = []
  const sock = openTerminalSocket('ws://h/attach', {
    onData: (b) => data.push(b),
    onExit: (c) => exits.push(c),
    onStatus: (s) => statuses.push(s),
    WebSocketImpl: impl,
  })
  return { sock, data, exits, statuses }
}

beforeEach(() => {
  FakeWebSocket.all = []
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('openTerminalSocket', () => {
  it('bytes_in_and_out', () => {
    const { sock, data, statuses } = setup()
    expect(statuses).toEqual(['connecting'])
    expect(last().url).toBe('ws://h/attach')
    expect(last().binaryType).toBe('arraybuffer')
    last().open()
    expect(statuses).toEqual(['connecting', 'open'])
    last().bytes(1, 2, 3)
    last().bytes(4)
    expect(data.map((d) => Array.from(d))).toEqual([[1, 2, 3], [4]])
    sock.send('hé')
    sock.send(new Uint8Array([9, 8]))
    const out = last().sent as Uint8Array[]
    expect(out.every((f) => f instanceof Uint8Array)).toBe(true)
    expect(dec.decode(out[0])).toBe('hé')
    expect(Array.from(out[0])).toEqual([0x68, 0xc3, 0xa9])
    expect(Array.from(out[1])).toEqual([9, 8])
  })

  it('resize_and_resend', () => {
    const { sock } = setup()
    sock.resize(100, 30)
    expect(last().sent).toEqual([])
    last().open()
    expect(last().sent).toEqual([JSON.stringify({ type: 'resize', cols: 100, rows: 30 })])
    sock.resize(120, 40)
    expect(last().sent[1]).toBe(JSON.stringify({ type: 'resize', cols: 120, rows: 40 }))
    last().drop(1006)
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.all).toHaveLength(2)
    last().open()
    expect(last().sent).toEqual([JSON.stringify({ type: 'resize', cols: 120, rows: 40 })])
  })

  it('ping_every_20s', () => {
    const { sock } = setup()
    last().open()
    vi.advanceTimersByTime(19999)
    expect(last().sent).toEqual([])
    vi.advanceTimersByTime(1)
    expect(last().sent).toEqual(['{"type":"ping"}'])
    vi.advanceTimersByTime(40000)
    expect(last().sent).toHaveLength(3)
    last().text({ type: 'pong' })
    sock.close()
    vi.advanceTimersByTime(60000)
    expect(last().sent).toHaveLength(3)
  })

  it('ping_stops_while_reconnecting', () => {
    setup()
    const first = last()
    first.open()
    first.drop(1006)
    vi.advanceTimersByTime(19000)
    expect(first.sent).toEqual([])
  })

  it('reconnect_policy', () => {
    const { statuses } = setup()
    last().open()
    const delays = [1000, 2000, 4000, 8000, 16000, 30000, 30000]
    delays.forEach((d, i) => {
      last().drop(1006)
      expect(statuses[statuses.length - 1]).toBe('reconnecting')
      vi.advanceTimersByTime(d - 1)
      expect(FakeWebSocket.all).toHaveLength(i + 1)
      vi.advanceTimersByTime(1)
      expect(FakeWebSocket.all).toHaveLength(i + 2)
    })
    // a frame proves the link: the backoff starts over
    last().open()
    last().bytes(1)
    last().drop(1006)
    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.all).toHaveLength(8)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.all).toHaveLength(9)
    last().open()
    expect(statuses[statuses.length - 1]).toBe('open')
  })

  it('no reconnect after 1000, 4404, 1008, exit or close()', () => {
    const cases: [number, TerminalStatus][] = [
      [1000, 'closed'],
      [4404, 'refused'],
      [1008, 'refused'],
    ]
    for (const [code, status] of cases) {
      FakeWebSocket.all = []
      const { statuses } = setup()
      last().open()
      last().drop(code)
      vi.advanceTimersByTime(120000)
      expect(FakeWebSocket.all, String(code)).toHaveLength(1)
      expect(statuses[statuses.length - 1], String(code)).toBe(status)
    }
    FakeWebSocket.all = []
    const a = setup()
    last().open()
    last().text({ type: 'exit', code: 0 })
    last().drop(1006)
    vi.advanceTimersByTime(120000)
    expect(FakeWebSocket.all).toHaveLength(1)
    expect(a.statuses[a.statuses.length - 1]).toBe('closed')

    FakeWebSocket.all = []
    const b = setup()
    last().open()
    b.sock.close()
    expect(last().closedWith).toBe(1000)
    last().drop(1006)
    vi.advanceTimersByTime(120000)
    expect(FakeWebSocket.all).toHaveLength(1)
    expect(b.statuses[b.statuses.length - 1]).toBe('closed')
  })

  it('close() while a reconnect is pending cancels it', () => {
    const { sock } = setup()
    last().open()
    last().drop(1006)
    sock.close()
    vi.advanceTimersByTime(120000)
    expect(FakeWebSocket.all).toHaveLength(1)
  })

  it('reconnect_close_codes', () => {
    for (const code of [1006, 1011, 1013, 4409]) {
      FakeWebSocket.all = []
      setup()
      last().open()
      last().drop(code)
      vi.advanceTimersByTime(1000)
      expect(FakeWebSocket.all, String(code)).toHaveLength(2)
    }
    for (const code of [1000, 1003, 1008, 1009]) {
      FakeWebSocket.all = []
      setup()
      last().open()
      last().drop(code)
      vi.advanceTimersByTime(120000)
      expect(FakeWebSocket.all, String(code)).toHaveLength(1)
    }
  })

  it('refused_after_ten_attempts', () => {
    const { statuses } = setup()
    last().open()
    last().drop(1006)
    for (let i = 0; i < 10; i++) {
      expect(statuses[statuses.length - 1]).toBe('reconnecting')
      vi.advanceTimersByTime(30000)
      expect(FakeWebSocket.all).toHaveLength(i + 2)
      last().drop(1006)
    }
    expect(statuses[statuses.length - 1]).toBe('refused')
    vi.advanceTimersByTime(120000)
    expect(FakeWebSocket.all).toHaveLength(11)
  })

  it('refuses after two closes before open', () => {
    const reasons: (string | undefined)[] = []
    const statuses: TerminalStatus[] = []
    openTerminalSocket('ws://h/attach', {
      onData: () => {},
      onExit: () => {},
      onStatus: (s, reason) => {
        statuses.push(s)
        reasons.push(reason)
      },
      WebSocketImpl: impl,
    })
    last().drop(1006)
    expect(statuses[statuses.length - 1]).toBe('reconnecting')
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.all).toHaveLength(2)
    last().drop(1006)
    expect(statuses[statuses.length - 1]).toBe('refused')
    expect(reasons[reasons.length - 1]).toBe('connection refused before open (check ORCH_ALLOWED_ORIGINS)')
    vi.advanceTimersByTime(120000)
    expect(FakeWebSocket.all).toHaveLength(2)
  })

  it('a socket that opened keeps the backoff after closes', () => {
    const { statuses } = setup()
    last().open()
    last().drop(1006)
    vi.advanceTimersByTime(1000)
    last().drop(1006)
    vi.advanceTimersByTime(2000)
    last().drop(1006)
    expect(statuses[statuses.length - 1]).toBe('reconnecting')
    expect(statuses).not.toContain('refused')
  })

  it('exit_frame', () => {
    const { data, exits, statuses } = setup()
    last().open()
    last().text({ type: 'exit', code: 7 })
    expect(exits).toEqual([7])
    expect(statuses[statuses.length - 1]).toBe('closed')
    expect(last().closedWith).toBe(1000)
    last().bytes(1)
    expect(data).toEqual([])
  })

  it('ignores pong and malformed text frames', () => {
    const { exits, data } = setup()
    last().open()
    last().text({ type: 'pong' })
    last().onmessage?.({ data: 'not json' })
    last().text({ type: 'unknown' })
    expect(exits).toEqual([])
    expect(data).toEqual([])
  })

  it('large_paste_split_into_frames', () => {
    expect(MAX_FRAME_BYTES).toBe(65536)
    const { sock } = setup()
    last().open()
    const big = new Uint8Array(MAX_FRAME_BYTES * 2 + 100).map((_, i) => i % 251)
    sock.send(big)
    const frames = last().sent as Uint8Array[]
    expect(frames.map((f) => f.length)).toEqual([MAX_FRAME_BYTES, MAX_FRAME_BYTES, 100])
    const joined = new Uint8Array(big.length)
    let at = 0
    for (const f of frames) {
      joined.set(f, at)
      at += f.length
    }
    expect(joined).toEqual(big)

    last().sent = []
    const text = 'aé'.repeat(MAX_FRAME_BYTES)
    sock.send(text)
    const tf = last().sent as Uint8Array[]
    expect(tf.length).toBeGreaterThan(1)
    expect(tf.every((f) => f.length <= MAX_FRAME_BYTES)).toBe(true)
    const all = new Uint8Array(tf.reduce((n, f) => n + f.length, 0))
    at = 0
    for (const f of tf) {
      all.set(f, at)
      at += f.length
    }
    expect(all).toEqual(new TextEncoder().encode(text))

    last().sent = []
    sock.send(new Uint8Array(MAX_FRAME_BYTES))
    sock.send('x')
    expect((last().sent as Uint8Array[]).map((f) => f.length)).toEqual([MAX_FRAME_BYTES, 1])
  })

  it('drops input while not open', () => {
    const { sock } = setup()
    sock.send('early')
    expect(last().sent).toEqual([])
  })
})

describe('terminalSocketUrl', () => {
  it('socket_url_from_location', () => {
    vi.stubGlobal('location', { protocol: 'https:', host: 'orch.example:8443' })
    expect(terminalSocketUrl('a1', 't-1')).toBe('wss://orch.example:8443/api/v1/agents/a1/terminals/t-1/attach')
    vi.stubGlobal('location', { protocol: 'http:', host: 'localhost:5173' })
    expect(terminalSocketUrl('a1', 't-1')).toBe('ws://localhost:5173/api/v1/agents/a1/terminals/t-1/attach')
  })
})
