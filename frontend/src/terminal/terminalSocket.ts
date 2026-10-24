/**
 * Browser end of the terminal attach protocol: binary frames carry PTY bytes both ways, text
 * frames carry JSON control. Reconnects abnormal closes with capped exponential backoff; a
 * reconnect is replayed the scrollback, so the caller resets its screen on every 'open' after
 * the first.
 */
import { BACKOFF_MAX_MS, BACKOFF_START_MS } from '../api/stream'

export type TerminalStatus = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'refused'

export interface TerminalSocketOptions {
  /** PTY output; the first frames after each 'open' are the scrollback replay. */
  onData: (bytes: Uint8Array) => void
  /** The shell exited with this code (null when the wrapper did not know it). */
  onExit: (code: number | null) => void
  onStatus: (status: TerminalStatus) => void
  /** Injectable for tests. */
  WebSocketImpl?: typeof WebSocket
}

export interface TerminalSocket {
  /** Input for the shell; dropped while the socket is not open. */
  send(data: string | Uint8Array): void
  /** Resizes the PTY, and again after every reconnect. */
  resize(cols: number, rows: number): void
  close(): void
}

/** The wrapper's default `terminal_max_frame_bytes`: a bigger frame is closed with 1009. */
export const MAX_FRAME_BYTES = 65536
/** Keeps ingress-nginx from closing an idle connection (its proxy timeout defaults to 60 s). */
export const PING_INTERVAL_MS = 20000
/** Reconnect attempts after one failure before the tab is refused. */
export const MAX_RECONNECT_ATTEMPTS = 10

const OPEN = 1
const NORMAL = 1000
// not a transient fault: unsupported data, policy violation, oversized frame, unknown terminal
const REFUSED_CODES = new Set([1003, 1008, 1009, 4404])

const encoder = new TextEncoder()

export function openTerminalSocket(url: string, opts: TerminalSocketOptions): TerminalSocket {
  const Impl = opts.WebSocketImpl ?? WebSocket
  let ws: WebSocket | null = null
  let finished = false
  let attempts = 0
  let delay = BACKOFF_START_MS
  let size: { cols: number; rows: number } | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let pingTimer: ReturnType<typeof setInterval> | null = null

  const isOpen = () => ws !== null && ws.readyState === OPEN
  const control = (message: object) => ws?.send(JSON.stringify(message))
  const stopPing = () => {
    if (pingTimer !== null) clearInterval(pingTimer)
    pingTimer = null
  }
  const finish = (status: TerminalStatus) => {
    finished = true
    stopPing()
    if (retryTimer !== null) clearTimeout(retryTimer)
    retryTimer = null
    opts.onStatus(status)
  }

  const onFrame = (e: MessageEvent) => {
    if (finished) return
    // a frame proves the link works: the next failure starts the backoff over
    attempts = 0
    delay = BACKOFF_START_MS
    if (typeof e.data !== 'string') {
      opts.onData(new Uint8Array(e.data as ArrayBuffer))
      return
    }
    let message: { type?: string; code?: number | null }
    try {
      message = JSON.parse(e.data)
    } catch {
      return
    }
    if (message.type === 'exit') {
      finish('closed')
      ws?.close(NORMAL)
      opts.onExit(message.code ?? null)
    }
  }

  const onClose = (code: number) => {
    stopPing()
    if (finished) return
    if (code === NORMAL) return finish('closed')
    if (REFUSED_CODES.has(code) || attempts >= MAX_RECONNECT_ATTEMPTS) return finish('refused')
    attempts += 1
    opts.onStatus('reconnecting')
    retryTimer = setTimeout(connect, delay)
    delay = Math.min(delay * 2, BACKOFF_MAX_MS)
  }

  function connect() {
    retryTimer = null
    const socket = new Impl(url)
    ws = socket
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => {
      if (finished || ws !== socket) return
      opts.onStatus('open')
      if (size) control({ type: 'resize', ...size })
      pingTimer = setInterval(() => control({ type: 'ping' }), PING_INTERVAL_MS)
    }
    socket.onmessage = (e) => {
      if (ws === socket) onFrame(e)
    }
    socket.onclose = (e) => {
      if (ws === socket) onClose(e.code)
    }
  }

  opts.onStatus('connecting')
  connect()

  return {
    send(data) {
      if (finished || !isOpen()) return
      const bytes = typeof data === 'string' ? encoder.encode(data) : data
      for (let at = 0; at < bytes.length; at += MAX_FRAME_BYTES) {
        ws?.send(bytes.slice(at, at + MAX_FRAME_BYTES))
      }
    },
    resize(cols, rows) {
      size = { cols, rows }
      if (!finished && isOpen()) control({ type: 'resize', cols, rows })
    },
    close() {
      if (finished) return
      finish('closed')
      ws?.close(NORMAL)
    },
  }
}
