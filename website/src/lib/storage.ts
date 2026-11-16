/**
 * Tiny preference store. Uses localStorage when the browser allows it and
 * silently falls back to memory (e.g. inside sandboxed preview iframes).
 */
const memory = new Map<string, string>();

function store(): Storage | null {
  try {
    const s = window.localStorage;
    const k = '__mip_probe__';
    s.setItem(k, k);
    s.removeItem(k);
    return s;
  } catch {
    return null;
  }
}

export function readPref(key: string): string | null {
  const s = store();
  try {
    return s ? s.getItem(key) : memory.get(key) ?? null;
  } catch {
    return memory.get(key) ?? null;
  }
}

export function writePref(key: string, value: string): boolean {
  memory.set(key, value);
  const s = store();
  if (!s) return false;
  try {
    s.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}
