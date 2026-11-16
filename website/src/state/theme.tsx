import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { readPref, writePref } from '../lib/storage';

export type ThemeId = 'forest' | 'blue';
export const THEMES: { id: ThemeId; label: string; description: string; swatch: string }[] = [
  { id: 'forest', label: 'Forest', description: 'Forest green theme', swatch: '#9CC5AC' },
  { id: 'blue', label: 'Blue', description: 'Dark blue theme', swatch: '#8CB4E8' },
];
const KEY = 'mip.theme';
const META: Record<ThemeId, string> = { forest: '#0B1713', blue: '#0B1220' };

/** A small content element near the top of the viewport, used to preserve reading position. */
function pickScrollAnchor(): { el: Element; top: number } | null {
  if (window.scrollY < 40) return null;
  const col = document.querySelector('.main-col') ?? document.querySelector('main');
  const r = col?.getBoundingClientRect();
  const x = r ? Math.min(r.left + 64, r.right - 8) : window.innerWidth / 2;
  for (const y of [120, 200, 300]) {
    const el = document.elementFromPoint(x, y);
    // Prefer a leaf block (paragraph, list item, heading); skip big containers.
    if (el && el !== document.body && el.getBoundingClientRect().height < window.innerHeight * 0.6) return { el, top: el.getBoundingClientRect().top };
  }
  return null;
}

const Ctx = createContext<{ theme: ThemeId; setTheme: (t: ThemeId) => void } | null>(null);

/**
 * Theme switching only changes `data-theme` on <html>. CSS variables do the
 * rest, so no component remounts and audio playback / scroll position are untouched.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeId>(() => {
    const saved = readPref(KEY);
    if (saved === 'forest' || saved === 'blue') return saved;
    const attr = document.documentElement.getAttribute('data-theme');
    return attr === 'blue' ? 'blue' : 'forest';
  });
  const anchor = useRef<{ el: Element; top: number } | null>(null);
  useLayoutEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', META[theme]);
    // The Forest homepage has a different layout height, so keep the same
    // content under the reader's eye instead of the same pixel offset.
    const a = anchor.current;
    anchor.current = null;
    if (a && a.el.isConnected) {
      // Take over from the browser's own scroll anchoring for this one change,
      // otherwise both adjustments would apply.
      const root = document.documentElement;
      root.style.overflowAnchor = 'none';
      const delta = a.el.getBoundingClientRect().top - a.top;
      if (Math.abs(delta) > 1) window.scrollBy(0, delta);
      requestAnimationFrame(() => requestAnimationFrame(() => (root.style.overflowAnchor = '')));
    }
  }, [theme]);
  const setTheme = useCallback((t: ThemeId) => {
    anchor.current = pickScrollAnchor();
    setThemeState(t);
    writePref(KEY, t);
  }, []);
  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme() {
  const c = useContext(Ctx);
  if (!c) throw new Error('useTheme outside ThemeProvider');
  return c;
}
