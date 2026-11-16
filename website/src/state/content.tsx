import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createHttpContentSource, MANIFEST_URL, type ContentSource } from '../content/source';
import type { Manifest } from '../content/types';

/**
 * Prototype-only content scenarios, selectable from the footer, so the
 * loading / empty / error / "episode not yet published" states can be reviewed.
 * Remove `scenario` handling once the real manifest is connected.
 */
export type Scenario = 'sample' | 'episode-pending' | 'loading' | 'empty' | 'error';

export const SCENARIOS: { id: Scenario; label: string }[] = [
  { id: 'sample', label: 'Sample manifest' },
  { id: 'episode-pending', label: 'Episode pending' },
  { id: 'loading', label: 'Loading' },
  { id: 'empty', label: 'Empty' },
  { id: 'error', label: 'Error' },
];

function applyScenario(m: Manifest, s: Scenario): Manifest {
  if (s === 'empty') return { ...m, editions: [] };
  if (s === 'episode-pending') {
    // Simulate: newest briefing is published, its episode is still rendering.
    const newest = [...m.editions]
      .filter((e) => e.briefing?.status === 'published')
      .sort((a, b) => Date.parse(b.briefing!.publishedAt) - Date.parse(a.briefing!.publishedAt))[0];
    return {
      ...m,
      editions: m.editions.map((e) =>
        e === newest && e.episode ? { ...e, episode: { ...e.episode, status: 'processing', publishedAt: null } } : e,
      ),
    };
  }
  return m;
}

type ContentState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; manifest: Manifest };

interface ContentCtx {
  state: ContentState;
  source: ContentSource;
  scenario: Scenario;
  setScenario: (s: Scenario) => void;
  retry: () => void;
}

const Ctx = createContext<ContentCtx | null>(null);

export function ContentProvider({ children }: { children: ReactNode }) {
  const realSource = useRef(createHttpContentSource(MANIFEST_URL)).current;
  const brokenSource = useRef(createHttpContentSource('content/does-not-exist.json')).current;
  const [scenario, setScenario] = useState<Scenario>('sample');
  const [state, setState] = useState<ContentState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const source = scenario === 'error' ? brokenSource : realSource;

  useEffect(() => {
    let cancelled = false;
    setState({ status: 'loading' });
    if (scenario === 'loading') return;
    source
      .loadManifest()
      .then((m) => !cancelled && setState({ status: 'ready', manifest: applyScenario(m, scenario) }))
      .catch((err: Error) => !cancelled && setState({ status: 'error', message: err?.message || 'Unknown error' }));
    return () => {
      cancelled = true;
    };
  }, [source, scenario, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  const value = useMemo(() => ({ state, source, scenario, setScenario, retry }), [state, source, scenario, retry]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useContent() {
  const c = useContext(Ctx);
  if (!c) throw new Error('useContent outside ContentProvider');
  return c;
}

/** Load a briefing body on demand (cached by the source). */
export function useBriefingBody(editionId: string | undefined) {
  const { source, state } = useContent();
  const [res, setRes] = useState<{ id?: string; blocks: import('../content/types').BriefingBlock[] | null; error: string | null }>({
    blocks: null,
    error: null,
  });
  const ready = state.status === 'ready';
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!editionId || !ready) return;
    let cancelled = false;
    setRes({ id: editionId, blocks: null, error: null });
    source
      .loadBriefingBody(editionId)
      .then((blocks) => !cancelled && setRes({ id: editionId, blocks, error: null }))
      .catch((e: Error) => !cancelled && setRes({ id: editionId, blocks: null, error: e?.message || 'Failed to load' }));
    return () => {
      cancelled = true;
    };
  }, [editionId, ready, source, n]);
  const current = res.id === editionId ? res : { blocks: null, error: null };
  return { ...current, loading: !current.blocks && !current.error, retry: () => setN((x) => x + 1) };
}
