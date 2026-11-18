import { useEffect, useLayoutEffect, useRef } from 'react';
import { dailyState } from './content/selectors';
import { href, useRoute } from './lib/router';
import { useContent } from './state/content';
import { Footer, NowPlayingBar, TopNav } from './components/Shell';
import { StateMessage } from './components/ui';
import { Dashboard } from './pages/Dashboard';
import { BriefingsPage, EpisodesPage } from './pages/Archive';
import { ArticlePage } from './pages/Article';

const TITLES: Record<string, string> = {
  dashboard: 'Dashboard',
  briefings: 'Briefings',
  briefing: 'Briefing',
  episodes: 'Episodes',
  'not-found': 'Not found',
};

export function App() {
  const route = useRoute();
  const { state } = useContent();
  const mainRef = useRef<HTMLElement>(null);
  const first = useRef(true);
  const routeKey = route.name === 'briefing' ? `briefing:${route.editionId}` : route.name;

  // Route scope for page-specific styling (the Forest homepage redesign is limited to the dashboard).
  useLayoutEffect(() => {
    document.documentElement.setAttribute('data-page', route.name === 'dashboard' ? 'dashboard' : 'other');
  }, [route.name]);

  // On navigation: start at the top and move focus to the main region for screen readers.
  useEffect(() => {
    document.title = `${TITLES[route.name]} — Market Intelligence Podcast`;
    if (first.current) {
      first.current = false;
      return;
    }
    window.scrollTo(0, 0);
    mainRef.current?.focus({ preventScroll: true });
  }, [routeKey, route.name]);

  // Hide the now-playing bar when the latest episode's full player is already on screen.
  const latestId =
    route.name === 'dashboard' && state.status === 'ready' ? dailyState(state.manifest).latestEpisode?.episodeId ?? null : null;

  return (
    <>
      <a className="skip-link" href="#main" onClick={(e) => (e.preventDefault(), mainRef.current?.focus())}>
        Skip to content
      </a>
      <TopNav route={route} />
      <main id="main" ref={mainRef} tabIndex={-1} className="main">
        {route.name === 'dashboard' && <Dashboard />}
        {route.name === 'briefings' && <BriefingsPage />}
        {route.name === 'briefing' && <ArticlePage key={route.editionId} editionId={route.editionId} />}
        {route.name === 'episodes' && <EpisodesPage />}
        {route.name === 'not-found' && (
          <div className="container page-pad">
            <StateMessage
              title="Page not found."
              action={
                <a className="btn btn-secondary" href={href.dashboard()}>
                  Go to dashboard
                </a>
              }
            >
              <p className="mono">{route.path}</p>
            </StateMessage>
          </div>
        )}
      </main>
      <NowPlayingBar hiddenForEpisodeId={latestId} />
      <Footer />
    </>
  );
}
