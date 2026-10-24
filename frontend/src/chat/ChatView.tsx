/** Header (slug, state, agents strip) and the log of one cluster. */
import { useMemo, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { AgentView, ClusterView } from '../api/client'
import { AgentsStrip } from './AgentsStrip'
import { ChatLog } from './ChatLog'
import { Composer } from './Composer'
import { QueueStrip } from './QueueStrip'
import { filterByAgents } from './chatModel'
import { useClusterChat } from './useClusterChat'
import { TOKENS } from '../theme'

export interface ChatViewProps {
  cluster: ClusterView
  agents: AgentView[]
  /** Live window size; defaults to the production MAX_ITEMS. */
  maxItems?: number
}

export function ChatView({ cluster, agents, maxItems }: ChatViewProps) {
  const [filter, setFilter] = useState<{ clusterId: string; ids: string[] }>({ clusterId: cluster.id, ids: [] })
  const selected = useMemo(() => (filter.clusterId === cluster.id ? filter.ids : []), [filter, cluster.id])
  const ordinals = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a.ordinal])), [agents])
  const chat = useClusterChat(cluster.id, ordinals, maxItems)
  const items = useMemo(() => filterByAgents(chat.items, selected), [chat.items, selected])
  const toggle = (id: string) => setFilter({ clusterId: cluster.id, ids: selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id] })
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 2, pb: 1, borderBottom: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
        <Typography variant="h5" component="h2">
          {cluster.slug}
        </Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {cluster.desired === 'deleting' ? 'deleting' : cluster.summary.state}
        </Typography>
        <AgentsStrip
          agents={agents}
          selected={selected}
          onToggle={toggle}
          autoCompactPct={cluster.summary.auto_compact_pct}
          hive={cluster.hive ? { clusterId: cluster.id, leaderEpoch: cluster.leader_epoch } : undefined}
        />
      </Box>
      {chat.error ? <Alert severity="error">{chat.error}</Alert> : null}
      {chat.loading ? (
        <Typography sx={{ color: 'text.secondary', pt: 1 }}>Loading messages</Typography>
      ) : (
        <ChatLog key={`${cluster.id}:${selected.join(',')}`} items={items} hasMoreBefore={chat.hasMoreBefore} loadingOlder={chat.loadingOlder} onLoadOlder={chat.loadOlder} />
      )}
      <QueueStrip clusterId={cluster.id} />
      <Composer key={cluster.id} clusterId={cluster.id} agents={agents} deleting={cluster.desired === 'deleting'} />
    </Box>
  )
}
