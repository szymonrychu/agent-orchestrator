/** Crash-proof localStorage JSON helpers. Keys are namespaced `orch:`. Neither function ever throws. */
const PREFIX = 'orch:'

export function loadJson<T>(key: string, fallback: T, valid?: (v: unknown) => v is T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    if (raw === null || raw === undefined) return fallback
    const value: unknown = JSON.parse(raw)
    if (valid && !valid(value)) return fallback
    return value as T
  } catch {
    return fallback
  }
}

export function saveJson(key: string, value: unknown): void {
  try {
    const raw = JSON.stringify(value)
    if (raw === undefined) return
    localStorage.setItem(PREFIX + key, raw)
  } catch {
    // private mode, quota, missing localStorage or unserialisable value: persistence is best effort
  }
}
