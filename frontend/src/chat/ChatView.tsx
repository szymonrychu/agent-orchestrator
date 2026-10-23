/** Header (slug, state), agent filter and the log of one cluster. */
import { useMemo, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { AgentView, ClusterView } from '../api/client'
import { AgentFilter } from './AgentFilter'
import { ChatLog } from './ChatLog'
import { Composer } from './Composer'
import { QueueStrip } from './QueueStrip'
import { filterByAgents } from './chatModel'
import { useClusterChat } from './useClusterChat'
import { TOKENS } from '../theme'

export interface ChatViewProps {
  cluster: ClusterView
  agents: AgentView[]
}

export function ChatView({ cluster, agents }: ChatViewProps) {
  const [filter, setFilter] = useState<{ clusterId: string; ids: string[] }>({ clusterId: cluster.id, ids: [] })
  const selected = useMemo(() => (filter.clusterId === cluster.id ? filter.ids : []), [filter, cluster.id])
  const ordinals = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a.ordinal])), [agents])
  const chat = useClusterChat(cluster.id, ordinals)
  const items = useMemo(() => filterByAgents(chat.items, selected), [chat.items, selected])
  const toggle = (id: string) => setFilter({ clusterId: cluster.id, ids: selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id] })
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, pb: 1, borderBottom: `${TOKENS.borderWidth}px solid ${TOKENS.border}` }}>
        <Typography variant="h5" component="h2">
          {cluster.slug}
        </Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {cluster.desired === 'deleting' ? 'deleting' : cluster.summary.state}
        </Typography>
        <AgentFilter agents={agents} selected={selected} onToggle={toggle} />
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
