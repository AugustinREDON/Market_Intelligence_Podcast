/**
 * Minimal hash router (no dependency). Hash routes work on any static host,
 * including S3 website endpoints and sandboxed previews, without rewrite rules.
 *
 *   #/                      Dashboard
 *   #/briefings             Briefing archive
 *   #/briefings/:editionId  Briefing article view
 *   #/episodes              Episode archive
 */
import { useEffect, useState } from 'react';

export type Route =
  | { name: 'dashboard' }
  | { name: 'briefings' }
  | { name: 'briefing'; editionId: string }
  | { name: 'episodes' }
  | { name: 'not-found'; path: string };

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#/, '');
  // Non-route fragments (e.g. a skip link's "#main") are not navigation.
  if (raw && !raw.startsWith('/')) return { name: 'dashboard' };
  const path = raw.replace(/\/+$/, '') || '/';
  if (path === '/' || path === '') return { name: 'dashboard' };
  if (path === '/briefings') return { name: 'briefings' };
  if (path === '/episodes') return { name: 'episodes' };
  const m = path.match(/^\/briefings\/([\w-]+)$/);
  if (m) return { name: 'briefing', editionId: m[1] };
  return { name: 'not-found', path };
}

export const href = {
  dashboard: () => '#/',
  briefings: () => '#/briefings',
  briefing: (id: string) => `#/briefings/${encodeURIComponent(id)}`,
  episodes: () => '#/episodes',
};

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseHash(window.location.hash));
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return route;
}

export function navigate(to: string) {
  if (window.location.hash !== to) window.location.hash = to;
}
