// Pure reducer turning stored orchestrator event rows into chat items (D14).

export interface EventRow {
  id: number;
  agent_id: string;
  type: string;
  ts: string;
  turn_id?: string | null;
  data: Record<string, unknown>;
}

export interface Usage {
  [key: string]: unknown;
}

interface Base {
  eventId: number;
  agentId: string;
  agentOrdinal: number | undefined;
  ts: string;
  turnId?: string | null;
}

export type ChatItem =
  | (Base & { kind: 'user_message'; text: string; origin: string })
  | (Base & { kind: 'peer'; text: string; from?: string })
  | (Base & { kind: 'assistant'; text: string })
  | (Base & { kind: 'thinking'; text: string; collapsed: boolean })
  | (Base & {
      kind: 'tool';
      toolUseId: string;
      name: string;
      input: unknown;
      status: 'running' | 'ok' | 'error';
      output?: string;
    })
  | (Base & { kind: 'denied'; name: string; reason: string })
  | (Base & { kind: 'turn_end'; usage?: Usage })
  | (Base & { kind: 'error'; message: string })
  | (Base & { kind: 'gap'; fromSeq?: number; toSeq?: number });

export type ChatKind = ChatItem['kind'];

interface ToolResult {
  output: string;
  isError: boolean;
}

export interface ChatState {
  items: ChatItem[];
  ordinals: Readonly<Record<string, number>>;
  seen: ReadonlySet<number>;
  /** tool results whose call is not (yet) in the window, by tool_use_id */
  orphanResults: Readonly<Record<string, ToolResult>>;
}

export const MAX_ITEMS = 2000;

export function emptyChat(ordinals: Readonly<Record<string, number>> = {}): ChatState {
  return { items: [], ordinals, seen: new Set(), orphanResults: {} };
}

const str = (v: unknown, fallback = ''): string =>
  typeof v === 'string' ? v : v == null ? fallback : typeof v === 'object' ? JSON.stringify(v) : String(v);

const USER_ORIGINS = new Set(['user', 'hook', 'restart', 'command']);

function toolItem(base: Base, d: Record<string, unknown>, res?: ToolResult): ChatItem {
  return {
    ...base,
    kind: 'tool',
    toolUseId: str(d.tool_use_id),
    name: str(d.name ?? d.tool_name),
    input: d.tool_input ?? d.input,
    status: res ? (res.isError ? 'error' : 'ok') : 'running',
    output: res?.output,
  };
}

function usageOf(d: Record<string, unknown>): Usage | undefined {
  const rec = d.record as Record<string, unknown> | undefined;
  return ((rec?.usage ?? d.usage) as Usage | undefined) ?? undefined;
}

/** Item for a row, or null when the row yields no standalone item. */
function toItem(row: EventRow, ordinals: Readonly<Record<string, number>>, orphans: Readonly<Record<string, ToolResult>>): ChatItem | null {
  const d = row.data ?? {};
  const base: Base = {
    eventId: row.id,
    agentId: row.agent_id,
    agentOrdinal: ordinals[row.agent_id],
    ts: row.ts,
    turnId: row.turn_id,
  };
  switch (row.type) {
    case 'user_message': {
      const origin = str(d.origin, 'user');
      const text = str(d.text ?? d.message);
      if (origin === 'peer') return { ...base, kind: 'peer', text, from: d.from == null ? undefined : str(d.from) };
      return { ...base, kind: 'user_message', text, origin: USER_ORIGINS.has(origin) ? origin : 'user' };
    }
    case 'peer_message_received':
      return { ...base, kind: 'peer', text: str(d.message ?? d.text), from: d.from == null ? undefined : str(d.from) };
    case 'assistant_text':
      return { ...base, kind: 'assistant', text: str(d.text) };
    case 'thinking':
      return { ...base, kind: 'thinking', text: str(d.text ?? d.thinking), collapsed: true };
    case 'tool_call':
      return toolItem(base, d, orphans[str(d.tool_use_id)]);
    case 'tool_denied':
      return { ...base, kind: 'denied', name: str(d.name ?? d.tool_name), reason: str(d.reason) };
    case 'turn_completed':
      return { ...base, kind: 'turn_end', usage: usageOf(d) };
    case 'turn_failed':
    case 'error':
      return { ...base, kind: 'error', message: str(d.error ?? d.message ?? d.reason, row.type) };
    case 'gap':
      return {
        ...base,
        kind: 'gap',
        fromSeq: typeof d.from_seq === 'number' ? d.from_seq : undefined,
        toSeq: typeof d.to_seq === 'number' ? d.to_seq : undefined,
      };
    default:
      return null;
  }
}

function merge(state: ChatState, rows: readonly EventRow[]): ChatState {
  const seen = new Set(state.seen);
  const fresh: EventRow[] = [];
  for (const r of rows) {
    if (seen.has(r.id)) continue;
    seen.add(r.id);
    fresh.push(r);
  }
  if (fresh.length === 0) return state;
  fresh.sort((a, b) => a.id - b.id);

  const orphans: Record<string, ToolResult> = { ...state.orphanResults };
  const items = state.items.slice();
  const results: EventRow[] = [];
  const added: ChatItem[] = [];
  // Results first, so a call in the same batch pairs regardless of order.
  for (const r of fresh) {
    if (r.type === 'tool_result') results.push(r);
  }
  for (const r of results) {
    const d = r.data ?? {};
    orphans[str(d.tool_use_id)] = {
      output: str(d.response ?? d.output ?? d.content),
      isError: d.is_error === true || d.status === 'error',
    };
  }
  for (const r of fresh) {
    if (r.type === 'tool_result') continue;
    const it = toItem(r, state.ordinals, orphans);
    if (it) added.push(it);
  }
  // Pair results with calls already in the window.
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind === 'tool' && it.status === 'running' && orphans[it.toolUseId]) {
      const res = orphans[it.toolUseId];
      items[i] = { ...it, status: res.isError ? 'error' : 'ok', output: res.output };
    }
  }
  for (const it of added) if (it.kind === 'tool') delete orphans[it.toolUseId];
  for (const it of items) if (it.kind === 'tool' && it.status !== 'running') delete orphans[it.toolUseId];

  const all = items.concat(added).sort((a, b) => a.eventId - b.eventId);
  return { ...state, items: all, seen, orphanResults: orphans };
}

/** Append rows (live or newer page); dedupes by event id, keeps id order. */
export function applyEvents(state: ChatState, rows: readonly EventRow[]): ChatState {
  return merge(state, rows);
}

/** Insert older rows; dedupes and keeps tool pairing across the page boundary. */
export function prependHistory(state: ChatState, rows: readonly EventRow[]): ChatState {
  return merge(state, rows);
}

/** Keep only the newest `max` items. */
export function trimWindow(state: ChatState, max = MAX_ITEMS): ChatState {
  if (state.items.length <= max) return state;
  return { ...state, items: state.items.slice(state.items.length - max) };
}

/** Items of the given agents; an empty list means all agents. */
export function filterByAgents(items: readonly ChatItem[], ids: readonly string[]): ChatItem[] {
  if (ids.length === 0) return items.slice();
  const set = new Set(ids);
  return items.filter((i) => set.has(i.agentId));
}
