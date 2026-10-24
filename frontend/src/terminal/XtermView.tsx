/** One xterm.js screen wired to one terminal attach socket: input, output, fit and reconnect replay. */
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import Box from '@mui/material/Box'
import { useEffect, useRef } from 'react'
import { MONO_FONT, TOKENS, terminalTheme } from '../theme'
import { openTerminalSocket } from './terminalSocket'
import type { TerminalStatus } from './terminalSocket'

export interface XtermViewProps {
  /** The attach WebSocket of the shell. A new url (or a remount) attaches afresh. */
  url: string
  onStatus?: (status: TerminalStatus, reason?: string) => void
}

/** Fills its parent; refits whenever the parent changes size (panel drag, minimize, maximize, tab shown). */
export function XtermView({ url, onStatus }: XtermViewProps) {
  const host = useRef<HTMLDivElement>(null)
  const onStatusRef = useRef(onStatus)
  useEffect(() => {
    onStatusRef.current = onStatus
  })

  useEffect(() => {
    const el = host.current
    if (!el) return
    const term = new Terminal({ theme: terminalTheme, fontFamily: MONO_FONT, fontSize: TOKENS.fontSizes.md, cursorBlink: true })
    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(el)

    let opens = 0
    const socket = openTerminalSocket(url, {
      onData: (bytes) => term.write(bytes),
      onExit: (code) => term.writeln(`\r\n[process exited with code ${code ?? 'unknown'}]`),
      onStatus: (status, reason) => {
        // every open after the first is a reconnect, and the wrapper replays the scrollback into it
        if (status === 'open' && ++opens > 1) term.reset()
        onStatusRef.current?.(status, reason)
      },
    })
    term.onData((data) => socket.send(data))

    const fit = () => {
      // a hidden host (inactive tab, minimized panel) has no client size; FitAddon would read its declared
      // 100% as 100px and shrink the PTY to a few cells, so wait until the host is shown again
      if (el.clientWidth === 0 || el.clientHeight === 0) return
      fitAddon.fit()
      socket.resize(term.cols, term.rows)
    }
    fit()
    const observer = new ResizeObserver(fit)
    observer.observe(el)
    return () => {
      observer.disconnect()
      socket.close()
      term.dispose()
    }
  }, [url])

  return <Box ref={host} data-testid="xterm-host" sx={{ width: '100%', height: '100%', overflow: 'hidden' }} />
}
