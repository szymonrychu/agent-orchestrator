/** EventSource wrapper: tracks the last event id, reconnects with capped exponential backoff. */

export interface StreamInfo {
  type: string;
  id: string;
}

export interface StreamOptions {
  onEvent: (data: unknown, info: StreamInfo) => void;
  onOpen?: () => void;
  onError?: () => void;
  afterId?: number;
  /** Named SSE events to listen to (EventSource has no wildcard); unnamed frames are always handled. */
  eventNames?: string[];
  /** Injectable for tests. */
  eventSource?: typeof EventSource;
}

export interface Stream {
  close(): void;
}

export const BACKOFF_START_MS = 1000;
export const BACKOFF_MAX_MS = 30000;

function withAfterId(url: string, afterId: number | undefined): string {
  if (afterId === undefined) return url;
  return `${url}${url.includes('?') ? '&' : '?'}after_id=${afterId}`;
}

export function openStream(url: string, opts: StreamOptions): Stream {
  const Source = opts.eventSource ?? EventSource;
  let lastId: number | undefined = opts.afterId;
  let delay = BACKOFF_START_MS;
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const handle = (type: string) => (e: MessageEvent) => {
    if (closed) return;
    delay = BACKOFF_START_MS;
    if (e.lastEventId !== '' && Number.isFinite(Number(e.lastEventId))) lastId = Number(e.lastEventId);
    let data: unknown;
    try {
      data = JSON.parse(e.data as string);
    } catch {
      data = e.data;
    }
    opts.onEvent(data, { type, id: e.lastEventId });
  };

  const connect = () => {
    timer = null;
    if (closed) return;
    const es = new Source(withAfterId(url, lastId));
    source = es;
    es.onopen = () => {
      if (!closed) opts.onOpen?.();
    };
    es.onmessage = handle('message');
    for (const name of opts.eventNames ?? []) es.addEventListener(name, handle(name) as EventListener);
    es.onerror = () => {
      es.close();
      if (closed || source !== es) return;
      opts.onError?.();
      timer = setTimeout(connect, delay);
      delay = Math.min(delay * 2, BACKOFF_MAX_MS);
    };
  };

  connect();

  return {
    close() {
      closed = true;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      source?.close();
    },
  };
}
