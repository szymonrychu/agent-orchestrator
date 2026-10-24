import { useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { ChatView } from './chat/ChatView'
import { DiscardClusterDialog } from './clusters/DiscardClusterDialog'
import { NewClusterDialog } from './clusters/NewClusterDialog'
import { FilesPanel } from './files/FilesPanel'
import type { ClusterView } from './api/client'
import { TopBar } from './layout/TopBar'
import { Workspace } from './layout/Workspace'
import { sortedAgents, useClusters } from './ribbon/useClusters'
import { PresetsPage } from './presets/PresetsPage'
import { TerminalPanel } from './terminal/TerminalPanel'
import { TOKENS } from './theme'

export const APP_NAME = 'Agent Orchestrator';

export default function App() {
  const { state, clusters, select } = useClusters()
  const [creating, setCreating] = useState(false)
  // The main area shows the selected cluster or a settings page; no router.
  const [view, setView] = useState<'cluster' | 'presets'>('cluster')
  const [discarding, setDiscarding] = useState<ClusterView | null>(null)
  // The first cluster by slug is selected until a tab is chosen, and again when the chosen one disappears.
  const selectedId = state.selectedId !== null && state.selectedId in state.clusters ? state.selectedId : (clusters[0]?.cluster.id ?? null)
  const selected = selectedId === null ? null : state.clusters[selectedId]
  const show = (id: string) => {
    setView('cluster')
    select(id)
  }
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100dvh' }}>
      <TopBar
        clusters={clusters}
        selectedId={selectedId}
        onSelect={show}
        onNew={() => setCreating(true)}
        onDiscard={setDiscarding}
        settingsCategories={[{ id: 'presets', label: 'Presets', onOpen: () => setView('presets') }]}
      />
      <Box component="main" aria-label={APP_NAME} sx={{ display: 'flex', flexDirection: 'column', flexGrow: 1, minWidth: 0, minHeight: 0, overflow: 'hidden', p: 2 }}>
        {state.error && (
          <Box role="alert" sx={{ fontSize: TOKENS.fontSizes.sm, color: 'error.main' }}>
            {state.error}
          </Box>
        )}
        {view === 'presets' ? (
          <PresetsPage onBack={() => setView('cluster')} />
        ) : selected ? (
          <Box sx={{ flexGrow: 1, minHeight: 0 }}>
            <Workspace
              clusterId={selected.cluster.id}
              files={<FilesPanel clusterId={selected.cluster.id} />}
              chat={<ChatView cluster={selected.cluster} agents={sortedAgents(selected)} />}
              terminal={<TerminalPanel cluster={selected.cluster} agents={sortedAgents(selected)} />}
            />
          </Box>
        ) : (
          state.loaded && !state.error && <Typography color="text.secondary">No clusters yet</Typography>
        )}
      </Box>
      <NewClusterDialog open={creating} onClose={() => setCreating(false)} onCreated={(c) => { setCreating(false); show(c.id) }} />
      {discarding && <DiscardClusterDialog open cluster={discarding} onClose={() => setDiscarding(null)} onDiscarded={() => setDiscarding(null)} />}
    </Box>
  )
}
