import { useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { ChatView } from './chat/ChatView'
import { ClusterForm } from './clusters/ClusterForm'
import { DiscardClusterDialog } from './clusters/DiscardClusterDialog'
import type { ClusterView } from './api/client'
import { TopBar } from './layout/TopBar'
import { sortedAgents, useClusters } from './ribbon/useClusters'
import { TOKENS } from './theme'

export const APP_NAME = 'Agent Orchestrator';

export default function App() {
  const { state, clusters, select } = useClusters()
  const [creating, setCreating] = useState(false)
  const [discarding, setDiscarding] = useState<ClusterView | null>(null)
  // The first cluster by slug is selected until a tab is chosen, and again when the chosen one disappears.
  const selectedId = state.selectedId !== null && state.selectedId in state.clusters ? state.selectedId : (clusters[0]?.cluster.id ?? null)
  const selected = selectedId === null ? null : state.clusters[selectedId]
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100dvh' }}>
      <TopBar clusters={clusters} selectedId={selectedId} onSelect={select} onNew={() => setCreating(true)} onDiscard={setDiscarding} settingsCategories={[]} />
      <Box component="main" aria-label={APP_NAME} sx={{ flexGrow: 1, minWidth: 0, minHeight: 0, overflow: 'auto', p: 2 }}>
        {state.error && (
          <Box role="alert" sx={{ fontSize: TOKENS.fontSizes.sm, color: 'error.main' }}>
            {state.error}
          </Box>
        )}
        {selected ? (
          <ChatView cluster={selected.cluster} agents={sortedAgents(selected)} />
        ) : (
          state.loaded && !state.error && <Typography color="text.secondary">No clusters yet</Typography>
        )}
      </Box>
      <ClusterForm open={creating} mode="create" onClose={() => setCreating(false)} onSaved={(c) => { setCreating(false); select(c.id) }} />
      {discarding && <DiscardClusterDialog open cluster={discarding} onClose={() => setDiscarding(null)} onDiscarded={() => setDiscarding(null)} />}
    </Box>
  )
}
