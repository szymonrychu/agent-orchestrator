/** Form state, inline validation and sparse spec mapping for the preset editor. Server stays the authority. */
import type { ClusterSpec, Meta } from '../api/client'

export interface EnvRow {
  key: string
  value: string
}

export interface FormValues {
  name: string
  replicas: string
  /** Empty means same as replicas. */
  maxReplicas: string
  static: boolean
  model: string
  effort: string
  peers: boolean
  workdir: string
  pvcSize: string
  env: EnvRow[]
  anyMcp: string
  initScripts: string[]
  maxAttempts: string
  autoCompact: string
  cpuRequest: string
  cpuLimit: string
  memRequest: string
  memLimit: string
  hookUrl: string
  /** Not editable in the form; carried so an edit does not drop a stored image. */
  image: string
}

export type FormErrors = Partial<Record<Exclude<keyof FormValues, 'env' | 'initScripts'>, string>> & { env?: (string | undefined)[]; initScripts?: (string | undefined)[] }

/** What the user set: a spec key, with the four resource fields counting as one `resources` key. */
export type SetKey = 'replicas' | 'maxReplicas' | 'static' | 'model' | 'effort' | 'peers' | 'workdir' | 'pvcSize' | 'env' | 'anyMcp' | 'initScripts' | 'maxAttempts' | 'autoCompact' | 'resources' | 'hookUrl' | 'image'

/** Form values together with the fields that are set (the ones a save writes to the preset). */
export interface FormState {
  values: FormValues
  setKeys: Set<SetKey>
}

const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/
const NAME_CHARS = /^[A-Za-z0-9 ._-]+$/
const SECRET_KEY = /TOKEN|SECRET|PASSW(OR)?D|CREDENTIAL|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY/i
const RESERVED_KEY = /^(CCW_|CLAUDE_|ANTHROPIC_)|^POD_NAME$|_PROXY$|BASE_URL$/i
export const MAX_SLUG = 40
export const MAX_NAME = 40
export const MAX_INIT_SCRIPTS = 20
export const MAX_INIT_SCRIPT_CHARS = 16384
const COPY_SUFFIX = ' copy'

export function blankValues(meta: Meta | null): FormValues {
  const d = meta?.defaults
  return {
    name: '', replicas: d ? String(d.replicas) : '', maxReplicas: '', static: false, model: d?.model ?? '', effort: d?.effort ?? '', peers: true, workdir: d?.workdir ?? 'workdir',
    pvcSize: d?.pvc_size ?? '', env: [], anyMcp: d && Object.keys(d.any_mcp_config).length > 0 ? JSON.stringify(d.any_mcp_config, null, 2) : '', initScripts: [],
    maxAttempts: d ? String(d.max_attempts) : '', autoCompact: d ? String(d.auto_compact_pct) : '',
    cpuRequest: d?.resources.requests.cpu ?? '', cpuLimit: d?.resources.limits.cpu ?? '',
    memRequest: d?.resources.requests.memory ?? '', memLimit: d?.resources.limits.memory ?? '', hookUrl: '', image: '',
  }
}

/** The set-key that owns a form field. */
export function setKeyOf(field: keyof FormValues): SetKey | null {
  if (field === 'name') return null
  return field === 'cpuRequest' || field === 'cpuLimit' || field === 'memRequest' || field === 'memLimit' ? 'resources' : field
}

/**
 * Form state for a stored preset spec: the stored value for every stored key, the /meta default
 * (`blankValues(meta)`) for every other field; only the stored keys are set. A stored null counts as not stored.
 */
export function stateFromPreset(meta: Meta | null, name: string, spec: Partial<ClusterSpec>): FormState {
  const values = { ...blankValues(meta), name }
  const setKeys = new Set<SetKey>()
  const text = (key: SetKey, field: 'model' | 'effort' | 'workdir' | 'pvcSize' | 'hookUrl' | 'image', stored: string | null | undefined) => {
    if (stored === null || stored === undefined) return
    values[field] = stored
    setKeys.add(key)
  }
  const num = (key: SetKey, field: 'replicas' | 'maxReplicas' | 'maxAttempts' | 'autoCompact', stored: number | null | undefined) => {
    if (stored === null || stored === undefined) return
    values[field] = String(stored)
    setKeys.add(key)
  }
  num('replicas', 'replicas', spec.replicas)
  num('maxReplicas', 'maxReplicas', spec.max_replicas)
  if (spec.static !== undefined && spec.static !== null) {
    values.static = spec.static
    setKeys.add('static')
  }
  text('model', 'model', spec.model)
  text('effort', 'effort', spec.effort)
  if (spec.peers !== undefined && spec.peers !== null) {
    values.peers = spec.peers
    setKeys.add('peers')
  }
  text('workdir', 'workdir', spec.workdir)
  text('pvcSize', 'pvcSize', spec.pvc_size)
  if (spec.env) {
    values.env = Object.entries(spec.env).map(([key, value]) => ({ key, value }))
    setKeys.add('env')
  }
  if (spec.any_mcp_config) {
    values.anyMcp = JSON.stringify(spec.any_mcp_config, null, 2)
    setKeys.add('anyMcp')
  }
  if (spec.init_scripts) {
    values.initScripts = [...spec.init_scripts]
    setKeys.add('initScripts')
  }
  num('maxAttempts', 'maxAttempts', spec.max_attempts)
  num('autoCompact', 'autoCompact', spec.auto_compact_pct)
  if (spec.resources) {
    values.cpuRequest = spec.resources.requests.cpu ?? ''
    values.memRequest = spec.resources.requests.memory ?? ''
    values.cpuLimit = spec.resources.limits.cpu ?? ''
    values.memLimit = spec.resources.limits.memory ?? ''
    setKeys.add('resources')
  }
  text('hookUrl', 'hookUrl', spec.discard_hook_url)
  text('image', 'image', spec.image)
  return { values, setKeys }
}

/** Name for a copy: the source cut so the whole name fits MAX_NAME, right-trimmed, then " copy". */
export function copyName(source: string): string {
  return source.slice(0, MAX_NAME - COPY_SUFFIX.length).trimEnd() + COPY_SUFFIX
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

/** Per-script errors mirroring the server (at most 20, each non-empty after trim and at most 16384 characters); undefined when all are fine. */
export function initScriptErrors(scripts: string[]): (string | undefined)[] | undefined {
  const errors = scripts.map((s, i): string | undefined => {
    if (i >= MAX_INIT_SCRIPTS) return `Init script ${i + 1}: at most ${MAX_INIT_SCRIPTS} init scripts`
    if (s.trim() === '') return `Init script ${i + 1} must not be empty`
    if (s.length > MAX_INIT_SCRIPT_CHARS) return `Init script ${i + 1} must be at most ${MAX_INIT_SCRIPT_CHARS} characters`
    return undefined
  })
  return errors.some(Boolean) ? errors : undefined
}

export function nameError(name: string): string | undefined {
  if (name === '') return 'Name is required'
  if (name.length > MAX_NAME) return `Name must be at most ${MAX_NAME} characters`
  if (name !== name.trim()) return 'Name must not start or end with a space'
  if (!NAME_CHARS.test(name)) return 'Name may hold letters, digits, spaces, dots, dashes and underscores'
  return undefined
}

/** Inline errors; an empty field is never an error because clearing a field means unset. */
export function validate(v: FormValues, meta: Meta | null): FormErrors {
  const e: FormErrors = {}
  e.name = nameError(v.name)
  e.replicas = v.replicas.trim() === '' ? undefined : intIn(v.replicas, 1, meta?.caps.max_replicas ?? 16, 'Replicas')
  const replicas = /^\d+$/.test(v.replicas.trim()) ? Number(v.replicas) : meta?.defaults.replicas ?? 1
  e.maxReplicas = v.maxReplicas.trim() === '' ? undefined : intIn(v.maxReplicas, Math.max(1, replicas), meta?.caps.max_replicas ?? 16, 'Max replicas')
  if (e.maxReplicas === undefined && v.static && v.maxReplicas.trim() !== '' && Number(v.maxReplicas) !== replicas) e.static = 'Static needs max replicas equal to replicas'
  e.maxAttempts = v.maxAttempts.trim() === '' ? undefined : intIn(v.maxAttempts, 1, 100, 'Max attempts')
  e.autoCompact = v.autoCompact.trim() === '' ? undefined : intIn(v.autoCompact, 1, 100, 'Auto-compact percent')
  e.anyMcp = anyMcpError(v.anyMcp)
  e.hookUrl = hookUrlError(v.hookUrl)
  const envErrors = v.env.map((r) => envKeyError(r.key, v.env))
  if (envErrors.some(Boolean)) e.env = envErrors
  const scriptErrors = initScriptErrors(v.initScripts)
  if (scriptErrors) e.initScripts = scriptErrors
  for (const k of Object.keys(e) as (keyof FormErrors)[]) if (e[k] === undefined) delete e[k]
  return e
}

function pairs(cpu: string, memory: string): Record<string, string> {
  const out: Record<string, string> = {}
  if (cpu.trim()) out.cpu = cpu.trim()
  if (memory.trim()) out.memory = memory.trim()
  return out
}

/**
 * Maps validated form values to a sparse preset spec: only the set fields, as ClusterSpec keys.
 * A set field that is empty is omitted (clearing means unset), so no key is ever null.
 */
export function toPresetSpec(v: FormValues, setKeys: ReadonlySet<SetKey>): Partial<ClusterSpec> {
  const spec: Record<string, unknown> = {}
  const str = (s: string): string | undefined => (s.trim() === '' ? undefined : s.trim())
  const int = (s: string): number | undefined => (s.trim() === '' ? undefined : Number(s))
  if (setKeys.has('replicas')) spec.replicas = int(v.replicas)
  if (setKeys.has('maxReplicas')) spec.max_replicas = int(v.maxReplicas)
  if (setKeys.has('static')) spec.static = v.static
  if (setKeys.has('model')) spec.model = str(v.model)
  if (setKeys.has('effort')) spec.effort = str(v.effort)
  if (setKeys.has('peers')) spec.peers = v.peers
  if (setKeys.has('workdir')) spec.workdir = str(v.workdir)
  if (setKeys.has('pvcSize')) spec.pvc_size = str(v.pvcSize)
  if (setKeys.has('env') && v.env.length > 0) spec.env = Object.fromEntries(v.env.map((r) => [r.key, r.value]))
  if (setKeys.has('anyMcp') && v.anyMcp.trim() !== '') spec.any_mcp_config = JSON.parse(v.anyMcp) as Record<string, unknown>
  if (setKeys.has('initScripts')) spec.init_scripts = [...v.initScripts]
  if (setKeys.has('maxAttempts')) spec.max_attempts = int(v.maxAttempts)
  if (setKeys.has('autoCompact')) spec.auto_compact_pct = int(v.autoCompact)
  if (setKeys.has('resources')) {
    const requests = pairs(v.cpuRequest, v.memRequest)
    const limits = pairs(v.cpuLimit, v.memLimit)
    if (Object.keys(requests).length + Object.keys(limits).length > 0) spec.resources = { requests, limits }
  }
  if (setKeys.has('hookUrl')) spec.discard_hook_url = str(v.hookUrl)
  if (setKeys.has('image')) spec.image = str(v.image)
  return Object.fromEntries(Object.entries(spec).filter(([, value]) => value !== undefined)) as Partial<ClusterSpec>
}
