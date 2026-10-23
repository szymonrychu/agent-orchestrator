/** Form state, inline validation and spec mapping for the cluster create and edit dialog. Server stays the authority. */
import type { ClusterSpec, Meta } from '../api/client'

export interface EnvRow {
  key: string
  value: string
}

export interface FormValues {
  slug: string
  replicas: string
  model: string
  effort: string
  peers: boolean
  workdir: string
  pvcSize: string
  env: EnvRow[]
  anyMcp: string
  maxAttempts: string
  autoCompact: string
  cpuRequest: string
  cpuLimit: string
  memRequest: string
  memLimit: string
  hookUrl: string
  /** Not editable in the form; carried so an edit does not reset it. */
  image: string
}

export type FormErrors = Partial<Record<Exclude<keyof FormValues, 'env'>, string>> & { env?: (string | undefined)[] }

const SLUG = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/
const SECRET_KEY = /TOKEN|SECRET|PASSW(OR)?D|CREDENTIAL|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY/i
const RESERVED_KEY = /^(CCW_|CLAUDE_|ANTHROPIC_)|^POD_NAME$|_PROXY$|BASE_URL$/i
export const MAX_SLUG = 40

export function blankValues(meta: Meta | null): FormValues {
  const d = meta?.defaults
  return {
    slug: '', replicas: d ? String(d.replicas) : '', model: d?.model ?? '', effort: d?.effort ?? '', peers: true, workdir: d?.workdir ?? 'workdir',
    pvcSize: d?.pvc_size ?? '', env: [], anyMcp: d && Object.keys(d.any_mcp_config).length > 0 ? JSON.stringify(d.any_mcp_config, null, 2) : '',
    maxAttempts: d ? String(d.max_attempts) : '', autoCompact: d ? String(d.auto_compact_pct) : '',
    cpuRequest: d?.resources.requests.cpu ?? '', cpuLimit: d?.resources.limits.cpu ?? '',
    memRequest: d?.resources.requests.memory ?? '', memLimit: d?.resources.limits.memory ?? '', hookUrl: '', image: '',
  }
}

export function valuesFromSpec(slug: string, spec: ClusterSpec): FormValues {
  const num = (n: number | null | undefined): string => (n === null || n === undefined ? '' : String(n))
  return {
    slug, replicas: num(spec.replicas), model: spec.model ?? '', effort: spec.effort ?? '', peers: spec.peers, workdir: spec.workdir,
    pvcSize: spec.pvc_size ?? '', env: Object.entries(spec.env).map(([key, value]) => ({ key, value })),
    anyMcp: spec.any_mcp_config ? JSON.stringify(spec.any_mcp_config, null, 2) : '', maxAttempts: num(spec.max_attempts), autoCompact: num(spec.auto_compact_pct),
    cpuRequest: spec.resources?.requests.cpu ?? '', cpuLimit: spec.resources?.limits.cpu ?? '',
    memRequest: spec.resources?.requests.memory ?? '', memLimit: spec.resources?.limits.memory ?? '', hookUrl: spec.discard_hook_url ?? '', image: spec.image ?? '',
  }
}

const intIn = (text: string, min: number, max: number, what: string): string | undefined => {
  if (!/^\d+$/.test(text.trim())) return `${what} must be a whole number`
  const n = Number(text)
  return n < min || n > max ? `${what} must be ${min} to ${max}` : undefined
}

export function envKeyError(key: string, all: EnvRow[]): string | undefined {
  if (key === '') return 'Key is required'
  if (!ENV_NAME.test(key)) return 'Key may hold letters, digits and underscores only'
  if (SECRET_KEY.test(key)) return 'Looks like a secret, secrets do not belong in the spec'
  if (RESERVED_KEY.test(key)) return 'Reserved name, set by the orchestrator'
  if (all.filter((r) => r.key === key).length > 1) return 'Duplicate key'
  return undefined
}

export function hookUrlError(text: string): string | undefined {
  if (text.trim() === '') return undefined
  try {
    const u = new URL(text.trim())
    return u.protocol === 'http:' || u.protocol === 'https:' ? undefined : 'Must be an http or https URL'
  } catch {
    return 'Must be an http or https URL'
  }
}

export function anyMcpError(text: string): string | undefined {
  if (text.trim() === '') return undefined
  try {
    const v: unknown = JSON.parse(text)
    return v !== null && typeof v === 'object' && !Array.isArray(v) ? undefined : 'Must be a JSON object'
  } catch {
    return 'Must be valid JSON'
  }
}

export function validate(v: FormValues, mode: 'create' | 'edit', meta: Meta | null): FormErrors {
  const e: FormErrors = {}
  if (mode === 'create') {
    if (v.slug === '') e.slug = 'Slug is required'
    else if (v.slug.length > MAX_SLUG) e.slug = `Slug must be at most ${MAX_SLUG} characters`
    else if (!SLUG.test(v.slug)) e.slug = 'Slug may hold lowercase letters, digits and hyphens, starting and ending with a letter or digit'
    if (v.pvcSize.trim() === '') e.pvcSize = 'PVC size is required'
  }
  e.replicas = intIn(v.replicas, 1, meta?.caps.max_replicas ?? 16, 'Replicas')
  e.maxAttempts = intIn(v.maxAttempts, 1, 100, 'Max attempts')
  e.autoCompact = v.autoCompact.trim() === '' ? undefined : intIn(v.autoCompact, 1, 100, 'Auto-compact percent')
  if (v.workdir.trim() === '') e.workdir = 'Workdir is required'
  e.anyMcp = anyMcpError(v.anyMcp)
  e.hookUrl = hookUrlError(v.hookUrl)
  const envErrors = v.env.map((r) => envKeyError(r.key, v.env))
  if (envErrors.some(Boolean)) e.env = envErrors
  for (const k of Object.keys(e) as (keyof FormErrors)[]) if (e[k] === undefined) delete e[k]
  return e
}

const orNull = (s: string): string | null => (s.trim() === '' ? null : s.trim())

function pairs(cpu: string, memory: string): Record<string, string> {
  const out: Record<string, string> = {}
  if (cpu.trim()) out.cpu = cpu.trim()
  if (memory.trim()) out.memory = memory.trim()
  return out
}

/** Maps validated form values to the API spec. */
export function toSpec(v: FormValues): ClusterSpec {
  const requests = pairs(v.cpuRequest, v.memRequest)
  const limits = pairs(v.cpuLimit, v.memLimit)
  const hasResources = Object.keys(requests).length + Object.keys(limits).length > 0
  return {
    replicas: Number(v.replicas), model: orNull(v.model), effort: orNull(v.effort), peers: v.peers, workdir: v.workdir.trim(), pvc_size: orNull(v.pvcSize),
    env: Object.fromEntries(v.env.map((r) => [r.key, r.value])),
    any_mcp_config: v.anyMcp.trim() === '' ? null : (JSON.parse(v.anyMcp) as Record<string, unknown>),
    max_attempts: Number(v.maxAttempts), auto_compact_pct: v.autoCompact.trim() === '' ? null : Number(v.autoCompact),
    resources: hasResources ? { requests, limits } : null, discard_hook_url: orNull(v.hookUrl),
    ...(v.image === '' ? {} : { image: v.image }),
  }
}
