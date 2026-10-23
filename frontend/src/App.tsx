import Typography from '@mui/material/Typography'
import { Shell } from './layout/Shell'
import { Ribbon } from './ribbon/Ribbon'
import { useClusters } from './ribbon/useClusters'

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
        <Typography variant="h5" component="h2">
          {selected.cluster.slug}
        </Typography>
      ) : (
        <Typography color="text.secondary">Select a cluster</Typography>
      )}
    </Shell>
  )
}
