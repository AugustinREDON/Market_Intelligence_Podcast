import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import type { EpisodeItem } from '../content/types';
import { useContent } from './content';

/**
 * One <audio> element for the whole app, owned by this provider. Every play
 * button talks to it, so only one episode can ever play at a time, and
 * theme changes, briefing expansion or route changes never restart playback.
 */

export const PLAYBACK_RATES = [0.75, 1, 1.25, 1.5, 1.75, 2] as const;

export interface PlayerSnapshot {
  episode: EpisodeItem | null;
  playing: boolean;
  buffering: boolean;
  currentTime: number;
  duration: number;
  rate: number;
  volume: number;
  muted: boolean;
  error: string | null;
}

interface PlayerApi {
  /** Start (or resume) an episode. Switching episodes stops the previous one. */
  play: (ep: EpisodeItem) => void;
  pause: () => void;
  toggle: (ep: EpisodeItem) => void;
  /** Seek within an episode, loading it (paused) if it is not the active one. */
  seek: (ep: EpisodeItem, seconds: number) => void;
  skip: (delta: number) => void;
  setRate: (r: number) => void;
  setVolume: (v: number) => void;
  toggleMute: () => void;
  resolveUrl: (u: string) => string;
  volumeSupported: boolean;
}

const ApiCtx = createContext<PlayerApi | null>(null);
const StoreCtx = createContext<{ subscribe: (cb: () => void) => () => void; get: () => PlayerSnapshot } | null>(null);

function detectVolumeSupport(): boolean {
  // iOS Safari ignores programmatic volume; reading it back reveals that.
  try {
    const a = document.createElement('audio');
    a.volume = 0.5;
    return a.volume === 0.5;
  } catch {
    return false;
  }
}

export function PlayerProvider({ children }: { children: ReactNode }) {
  const { source } = useContent();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const pendingSeek = useRef<number | null>(null);
  const snap = useRef<PlayerSnapshot>({
    episode: null,
    playing: false,
    buffering: false,
    currentTime: 0,
    duration: 0,
    rate: 1,
    volume: 1,
    muted: false,
    error: null,
  });
  const listeners = useRef(new Set<() => void>());
  const emit = (patch: Partial<PlayerSnapshot>) => {
    snap.current = { ...snap.current, ...patch };
    listeners.current.forEach((l) => l());
  };
  const store = useMemo(
    () => ({
      subscribe: (cb: () => void) => {
        listeners.current.add(cb);
        return () => listeners.current.delete(cb);
      },
      get: () => snap.current,
    }),
    [],
  );
  const [volumeSupported] = useState(detectVolumeSupport);

  useEffect(() => {
    const a = audioRef.current!;
    const on = (type: string, fn: () => void) => {
      a.addEventListener(type, fn);
      return () => a.removeEventListener(type, fn);
    };
    const offs = [
      on('play', () => emit({ playing: true, error: null })),
      on('pause', () => emit({ playing: false })),
      on('ended', () => emit({ playing: false })),
      on('waiting', () => emit({ buffering: true })),
      on('playing', () => emit({ buffering: false })),
      on('canplay', () => emit({ buffering: false })),
      on('timeupdate', () => emit({ currentTime: a.currentTime })),
      on('loadedmetadata', () => {
        emit({ duration: Number.isFinite(a.duration) ? a.duration : snap.current.duration });
        if (pendingSeek.current != null) {
          a.currentTime = pendingSeek.current;
          pendingSeek.current = null;
        }
      }),
      on('durationchange', () => Number.isFinite(a.duration) && emit({ duration: a.duration })),
      on('ratechange', () => emit({ rate: a.playbackRate })),
      on('volumechange', () => emit({ volume: a.volume, muted: a.muted })),
      on('error', () =>
        emit({ playing: false, buffering: false, error: 'This audio file could not be loaded. Check the connection or try the download link.' }),
      ),
    ];
    return () => offs.forEach((f) => f());
  }, []);

  const load = useCallback(
    (ep: EpisodeItem) => {
      const a = audioRef.current!;
      if (snap.current.episode?.episodeId === ep.episodeId) return a;
      if (!ep.audioUrl) return null;
      a.pause();
      const rate = a.playbackRate;
      a.src = source.resolveUrl(ep.audioUrl);
      a.load();
      a.playbackRate = rate; // keep the listener's chosen speed across episodes
      emit({ episode: ep, currentTime: 0, duration: ep.audioDurationSeconds ?? 0, playing: false, error: null, buffering: false });
      return a;
    },
    [source],
  );

  const api = useMemo<PlayerApi>(
    () => ({
      play: (ep) => {
        const a = load(ep);
        if (!a) return;
        emit({ buffering: a.readyState < 3 });
        a.play().catch((e: Error) => {
          if (e?.name !== 'AbortError') emit({ playing: false, buffering: false, error: 'Playback could not start. Please try again.' });
        });
      },
      pause: () => audioRef.current?.pause(),
      toggle: (ep) => {
        const a = audioRef.current!;
        if (snap.current.episode?.episodeId === ep.episodeId && !a.paused) a.pause();
        else api.play(ep);
      },
      seek: (ep, s) => {
        const a = load(ep);
        if (!a) return;
        if (a.readyState >= 1) a.currentTime = s;
        else pendingSeek.current = s;
        emit({ currentTime: s });
      },
      skip: (d) => {
        const a = audioRef.current!;
        if (!snap.current.episode) return;
        const dur = Number.isFinite(a.duration) ? a.duration : snap.current.duration;
        a.currentTime = Math.min(Math.max(0, a.currentTime + d), dur || 0);
      },
      setRate: (r) => {
        audioRef.current!.playbackRate = r;
        emit({ rate: r });
      },
      setVolume: (v) => {
        const a = audioRef.current!;
        a.volume = v;
        if (v > 0 && a.muted) a.muted = false;
      },
      toggleMute: () => {
        const a = audioRef.current!;
        a.muted = !a.muted;
      },
      resolveUrl: source.resolveUrl,
      volumeSupported,
    }),
    [load, source, volumeSupported],
  );

  return (
    <ApiCtx.Provider value={api}>
      <StoreCtx.Provider value={store}>
        {children}
        {/* preload="none": nothing is fetched or played until the listener asks. */}
        <audio ref={audioRef} preload="none" hidden data-testid="audio-element" />
      </StoreCtx.Provider>
    </ApiCtx.Provider>
  );
}

export function usePlayer() {
  const c = useContext(ApiCtx);
  if (!c) throw new Error('usePlayer outside PlayerProvider');
  return c;
}

export function usePlayerState(): PlayerSnapshot {
  const s = useContext(StoreCtx);
  if (!s) throw new Error('usePlayerState outside PlayerProvider');
  return useSyncExternalStore(s.subscribe, s.get, s.get);
}

/** Convenience: state for one particular episode. */
export function useEpisodeState(ep: EpisodeItem | null) {
  const s = usePlayerState();
  const active = !!ep && s.episode?.episodeId === ep.episodeId;
  return {
    active,
    playing: active && s.playing,
    buffering: active && s.buffering,
    currentTime: active ? s.currentTime : 0,
    duration: active && s.duration ? s.duration : ep?.audioDurationSeconds ?? 0,
    rate: s.rate,
    volume: s.volume,
    muted: s.muted,
    error: active ? s.error : null,
  };
}
