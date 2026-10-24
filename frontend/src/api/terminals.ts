/** Typed access to the shell terminal API and the attach socket URL. */
import { BASE, request } from './client'
import type { components } from './schema'

export type TerminalView = components['schemas']['TerminalView']
export type TerminalList = components['schemas']['TerminalList']

const id = encodeURIComponent

/** Starts a shell in the agent's session workdir; omitted sizes take the wrapper's defaults. */
export const createTerminal = (agentId: string, size: { rows?: number; cols?: number } = {}) =>
  request<TerminalView>('POST', `/agents/${id(agentId)}/terminals`, size)

/** Every shell of the cluster's ready agents; failing agents come back in `unavailable_agents`. */
export const listTerminals = (clusterId: string) =>
  request<TerminalList>('GET', `/clusters/${id(clusterId)}/terminals`)

/** Closes one shell. */
export const deleteTerminal = (agentId: string, terminalId: string) =>
  request<void>('DELETE', `/agents/${id(agentId)}/terminals/${id(terminalId)}`)

/** The attach WebSocket of one shell, on the page's own host and scheme. */
export const terminalSocketUrl = (agentId: string, terminalId: string) =>
  `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${BASE}/agents/${id(agentId)}/terminals/${id(terminalId)}/attach`
