import Typography from '@mui/material/Typography'
import { Shell } from './layout/Shell'
import { Ribbon } from './ribbon/Ribbon'
import { ChatView } from './chat/ChatView'
import { sortedAgents, useClusters } from './ribbon/useClusters'

export const APP_NAME = 'Agent Orchestrator';

export default function App() {
  const { state, clusters, select } = useClusters()
  const selected = state.selectedId === null ? null : state.clusters[state.selectedId]
  return (
    <Shell
      title={APP_NAME}
      aside={(close) => (
        <Ribbon
          clusters={clusters}
          selectedId={state.selectedId}
          error={state.error}
          onSelect={(id) => {
            select(id)
            close()
          }}
        />
      )}
    >
      {selected ? (
        <ChatView cluster={selected.cluster} agents={sortedAgents(selected)} />
      ) : (
        <Typography color="text.secondary">Select a cluster</Typography>
      )}
    </Shell>
  )
}
