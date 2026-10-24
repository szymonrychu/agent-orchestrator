/** Terminal panel: one tab per interactive shell of the cluster, each an xterm.js screen on a chosen agent pod. */
import Add from '@mui/icons-material/Add'
import Close from '@mui/icons-material/Close'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import Typography from '@mui/material/Typography'
import { useCallback, useEffect, useState } from 'react'
import type { MouseEvent } from 'react'
import { ApiError } from '../api/client'
import type { AgentView, ClusterView } from '../api/client'
import { createTerminal, deleteTerminal, listTerminals, terminalSocketUrl } from '../api/terminals'
import type { TerminalView } from '../api/terminals'
import { TOKENS } from '../theme'
import type { TerminalStatus } from './terminalSocket'
import { XtermView } from './XtermView'

export interface TerminalPanelProps {
  cluster: ClusterView
  /** The cluster's live agents ordered by ordinal. */
  agents: AgentView[]
}

interface Tab {
  agentId: string
  terminalId: string
  label: string
  /** Bumped by Retry to attach afresh. */
  attempt: number
  refused: boolean
}

const SHORT_ID = 6

const toTab = (t: TerminalView): Tab => ({ agentId: t.agent_id, terminalId: t.terminal_id, label: `${t.pod_name} ${t.terminal_id.slice(0, SHORT_ID)}`, attempt: 0, refused: false })

function errorText(e: unknown): string {
  if (e instanceof ApiError) return `${e.code}: ${e.message}`
  return e instanceof Error ? e.message : String(e)
}

/** Keyed by cluster, so a cluster change closes the old sockets and lists the new cluster's shells from scratch. */
export function TerminalPanel({ cluster, agents }: TerminalPanelProps) {
  return <ClusterTerminals key={cluster.id} clusterId={cluster.id} agents={agents} />
}

function ClusterTerminals({ clusterId, agents }: { clusterId: string; agents: AgentView[] }) {
  const [tabs, setTabs] = useState<Tab[]>([])
  const [active, setActive] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)

  // Shells outlive the panel (tab switches, reloads): reopen every alive one of the cluster.
  useEffect(() => {
    let live = true
    listTerminals(clusterId).then(
      (list) => {
        if (!live) return
        const opened = list.terminals.filter((t) => t.alive).map(toTab)
        setTabs(opened)
        setActive(opened[0]?.terminalId ?? null)
      },
      (e) => live && setError(errorText(e)),
    )
    return () => {
      live = false
    }
  }, [clusterId])

  const preselected = agents.find((a) => a.ready)?.id

  const create = async (agent: AgentView) => {
    setMenuAnchor(null)
    setError(null)
    try {
      const tab = toTab(await createTerminal(agent.id))
      setTabs((prev) => [...prev, tab])
      setActive(tab.terminalId)
    } catch (e) {
      setError(errorText(e))
    }
  }

  const close = async (tab: Tab) => {
    setError(null)
    try {
      await deleteTerminal(tab.agentId, tab.terminalId)
    } catch (e) {
      // a shell that is already gone only needs its tab dropped
      if (!(e instanceof ApiError && e.status === 404)) return setError(errorText(e))
    }
    setTabs((prev) => {
      const index = prev.findIndex((t) => t.terminalId === tab.terminalId)
      const rest = prev.filter((t) => t.terminalId !== tab.terminalId)
      setActive((current) => (current === tab.terminalId ? (rest[Math.min(index, rest.length - 1)]?.terminalId ?? null) : current))
      return rest
    })
  }

  const patch = useCallback((terminalId: string, change: Partial<Tab> | ((t: Tab) => Partial<Tab>)) => {
    setTabs((prev) => prev.map((t) => (t.terminalId === terminalId ? { ...t, ...(typeof change === 'function' ? change(t) : change) } : t)))
  }, [])

  const onStatus = useCallback(
    (terminalId: string, status: TerminalStatus) => patch(terminalId, { refused: status === 'refused' }),
    [patch],
  )

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, minWidth: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', borderBottom: 1, borderColor: 'divider', minWidth: 0 }}>
        <Tabs value={active ?? false} onChange={(_e, id: string) => setActive(id)} variant="scrollable" scrollButtons="auto" sx={{ flexGrow: 1, minWidth: 0 }}>
          {tabs.map((t) => (
            <Tab
              key={t.terminalId}
              value={t.terminalId}
              component="div"
              label={
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <span>{t.label}</span>
                  <IconButton
                    component="span"
                    size="small"
                    aria-label={`Close ${t.label}`}
                    onClick={(e: MouseEvent) => {
                      e.stopPropagation()
                      void close(t)
                    }}
                  >
                    <Close fontSize="inherit" />
                  </IconButton>
                </Box>
              }
            />
          ))}
        </Tabs>
        <Button size="small" startIcon={<Add />} onClick={(e) => setMenuAnchor(e.currentTarget)} sx={{ flexShrink: 0, mx: 1 }}>
          New terminal
        </Button>
        <Menu anchorEl={menuAnchor} open={menuAnchor !== null} onClose={() => setMenuAnchor(null)}>
          {agents.map((a) => (
            <MenuItem key={a.id} selected={a.id === preselected} autoFocus={a.id === preselected} disabled={!a.ready} onClick={() => void create(a)}>
              {a.pod_name}
            </MenuItem>
          ))}
          {agents.length === 0 && <MenuItem disabled>No agents</MenuItem>}
        </Menu>
      </Box>
      {error && (
        <Box role="alert" sx={{ px: 2, py: 1, fontSize: `${TOKENS.fontSizes.sm / 16}rem`, color: 'error.main' }}>
          {error}
        </Box>
      )}
      {tabs.length === 0 && !error && (
        <Typography color="text.secondary" sx={{ p: 2 }}>
          No terminals
        </Typography>
      )}
      <Box sx={{ position: 'relative', flexGrow: 1, minHeight: 0 }}>
        {tabs.map((t) => (
          <Box key={t.terminalId} role="tabpanel" hidden={t.terminalId !== active} sx={{ position: 'absolute', inset: 0, display: t.terminalId === active ? 'flex' : 'none', flexDirection: 'column' }}>
            {t.refused && (
              <Box data-testid="terminal-refused" sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 2, py: 1, fontSize: `${TOKENS.fontSizes.sm / 16}rem`, color: 'error.main' }}>
                <span>Connection refused: the terminal could not be reached.</span>
                <Button size="small" onClick={() => patch(t.terminalId, (p) => ({ attempt: p.attempt + 1, refused: false }))}>
                  Retry
                </Button>
                <Button size="small" onClick={() => void close(t)}>
                  Close
                </Button>
              </Box>
            )}
            <Box sx={{ flexGrow: 1, minHeight: 0 }}>
              <XtermView key={t.attempt} url={terminalSocketUrl(t.agentId, t.terminalId)} onStatus={(s) => onStatus(t.terminalId, s)} />
            </Box>
          </Box>
        ))}
      </Box>
    </Box>
  )
}
