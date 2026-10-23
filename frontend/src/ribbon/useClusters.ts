/** Hand-rolled cluster store for the top bar: initial REST load, live /api/v1/stream deltas, selected cluster id. */
import { useCallback, useEffect, useReducer } from 'react'
import { listAgents, listClusters, statusStreamUrl } from '../api/client'
import type { AgentView, ClusterView } from '../api/client'
import { openStream } from '../api/stream'

export interface ClusterEntry {
  cluster: ClusterView
  /** Agents by id, ordered by ordinal when read through `sortedAgents`. */
  agents: Record<string, AgentView>
}

export interface ClustersState {
  clusters: Record<string, ClusterEntry>
  selectedId: string | null
  loaded: boolean
  error: string | null
}

export type ClustersAction =
  | { type: 'loaded'; entries: ClusterEntry[] }
  | { type: 'failed'; message: string }
  | { type: 'snapshot'; clusters: { cluster: ClusterView; agents: AgentView[] }[] }
  | { type: 'cluster_status'; cluster: ClusterView }
  | { type: 'agent_status'; clusterId: string; agent: AgentView }
  | { type: 'message_status'; clusterId: string; queued: number; failed: number }
  | { type: 'cluster_removed'; clusterId: string }
  | { type: 'select'; id: string | null }

export const initialState: ClustersState = { clusters: {}, selectedId: null, loaded: false, error: null }

const byId = (agents: AgentView[]): Record<string, AgentView> => Object.fromEntries(agents.map((a) => [a.id, a]))
const toRecord = (entries: { cluster: ClusterView; agents: AgentView[] }[]): Record<string, ClusterEntry> =>
  Object.fromEntries(entries.map((e) => [e.cluster.id, { cluster: e.cluster, agents: byId(e.agents) }]))

/** The selection survives only while its cluster exists. */
function keepSelection(state: ClustersState, clusters: Record<string, ClusterEntry>): string | null {
  return state.selectedId !== null && state.selectedId in clusters ? state.selectedId : null
}

export function clustersReducer(state: ClustersState, action: ClustersAction): ClustersState {
  switch (action.type) {
    case 'loaded': {
      // The stream may already have delivered fresher data: only fill in what it has not.
      const loaded = toRecord(action.entries.map((e) => ({ cluster: e.cluster, agents: Object.values(e.agents) })))
      const clusters = state.loaded ? state.clusters : { ...loaded, ...state.clusters }
      return { ...state, clusters, loaded: true, error: null, selectedId: keepSelection(state, clusters) }
    }
    case 'failed':
      return { ...state, error: action.message, loaded: true }
    case 'snapshot': {
      const clusters = toRecord(action.clusters)
      return { ...state, clusters, loaded: true, error: null, selectedId: keepSelection(state, clusters) }
    }
    case 'cluster_status': {
      const prev = state.clusters[action.cluster.id]
      return { ...state, clusters: { ...state.clusters, [action.cluster.id]: { cluster: action.cluster, agents: prev?.agents ?? {} } } }
    }
    case 'agent_status': {
      const prev = state.clusters[action.clusterId]
      if (!prev) return state
      return { ...state, clusters: { ...state.clusters, [action.clusterId]: { ...prev, agents: { ...prev.agents, [action.agent.id]: action.agent } } } }
    }
    case 'message_status': {
      const prev = state.clusters[action.clusterId]
      if (!prev) return state
      const summary = { ...prev.cluster.summary, queued: action.queued, failed: action.failed }
      return { ...state, clusters: { ...state.clusters, [action.clusterId]: { ...prev, cluster: { ...prev.cluster, summary } } } }
    }
    case 'cluster_removed': {
      if (!(action.clusterId in state.clusters)) return state
      const clusters = { ...state.clusters }
      delete clusters[action.clusterId]
      return { ...state, clusters, selectedId: keepSelection(state, clusters) }
    }
    case 'select':
      return { ...state, selectedId: action.id }
  }
}

/** Clusters ordered by slug. */
export const sortedClusters = (state: ClustersState): ClusterEntry[] =>
  Object.values(state.clusters).sort((a, b) => a.cluster.slug.localeCompare(b.cluster.slug))

/** Live agents of a cluster ordered by ordinal; agents whose pod is gone are hidden. */
export const sortedAgents = (entry: ClusterEntry): AgentView[] =>
  Object.values(entry.agents)
    .filter((a) => !a.gone)
    .sort((a, b) => a.ordinal - b.ordinal)

const STREAM_EVENTS = ['snapshot', 'cluster_status', 'agent_status', 'message_status', 'cluster_removed']

/** Maps one decoded stream frame to a reducer action; null for frames the app ignores. */
export function actionFromEvent(type: string, data: unknown): ClustersAction | null {
  const d = data as Record<string, unknown> | null
  if (d === null || typeof d !== 'object') return null
  switch (type) {
    case 'snapshot':
      return { type: 'snapshot', clusters: d.clusters as { cluster: ClusterView; agents: AgentView[] }[] }
    case 'cluster_status':
      return { type: 'cluster_status', cluster: d.cluster as ClusterView }
    case 'agent_status':
      return { type: 'agent_status', clusterId: d.cluster_id as string, agent: d.agent as AgentView }
    case 'message_status':
      return { type: 'message_status', clusterId: d.cluster_id as string, queued: d.queued as number, failed: d.failed as number }
    case 'cluster_removed':
      return { type: 'cluster_removed', clusterId: d.cluster_id as string }
    default:
      return null
  }
}

export function useClusters() {
  const [state, dispatch] = useReducer(clustersReducer, initialState)

  useEffect(() => {
    let cancelled = false
    const stream = openStream(statusStreamUrl(), {
      eventNames: STREAM_EVENTS,
      onEvent: (data, info) => {
        const action = actionFromEvent(info.type, data)
        if (action && !cancelled) dispatch(action)
      },
    })
    listClusters()
      .then((clusters) => Promise.all(clusters.map(async (cluster) => ({ cluster, agents: byId(await listAgents(cluster.id)) }))))
      .then((entries) => {
        if (!cancelled) dispatch({ type: 'loaded', entries })
      })
      .catch((e: unknown) => {
        if (!cancelled) dispatch({ type: 'failed', message: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      cancelled = true
      stream.close()
    }
  }, [])

  const select = useCallback((id: string | null) => dispatch({ type: 'select', id }), [])
  return { state, clusters: sortedClusters(state), select }
}
