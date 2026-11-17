import type { Route } from '../lib/router';
import { href } from '../lib/router';
import { formatDuration } from '../lib/format';
import { SCENARIOS, useContent } from '../state/content';
import { usePlayer, usePlayerState } from '../state/player';
import { THEMES, useTheme } from '../state/theme';
import { PauseIcon, PlayIcon } from './Icons';

export function Wordmark() {
  return (
    <a href={href.dashboard()} className="wordmark" aria-label="Market Intelligence Podcast — Dashboard" data-testid="link-wordmark">
      <img
        src={`${import.meta.env.BASE_URL}branding/mip-logo.png`}
        className="wm-logo"
        alt=""
        aria-hidden="true"
      />
      <span className="wm-text" aria-hidden="true">
        <span className="wm-main">Market Intelligence</span>
        <span className="wm-sub">Podcast</span>
      </span>
    </a>
  );
}

export function ThemeSelector() {
  const { theme, setTheme } = useTheme();
  return (
    <fieldset className="theme-select" data-testid="theme-selector">
      <legend className="theme-legend">Theme</legend>
      <div className="seg">
        {THEMES.map((t) => (
          <label key={t.id} className="seg-opt" title={t.description}>
            <input
              type="radio"
              name="theme"
              value={t.id}
              checked={theme === t.id}
              onChange={() => setTheme(t.id)}
              data-testid={`radio-theme-${t.id}`}
            />
            <span className="seg-face">
              <span className="swatch" style={{ background: t.swatch }} aria-hidden="true" />
              <span>{t.label}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

const NAV: { name: string; to: string; match: Route['name'][] }[] = [
  { name: 'Dashboard', to: href.dashboard(), match: ['dashboard'] },
  { name: 'Briefings', to: href.briefings(), match: ['briefings', 'briefing'] },
  { name: 'Episodes', to: href.episodes(), match: ['episodes'] },
];

export function TopNav({ route }: { route: Route }) {
  return (
    <header className="topbar">
      <div className="container topbar-inner">
        <Wordmark />
        <nav className="mainnav" aria-label="Main">
          <ul>
            {NAV.map((n) => {
              const current = n.match.includes(route.name);
              return (
                <li key={n.name}>
                  <a href={n.to} aria-current={current ? 'page' : undefined} data-testid={`nav-${n.name.toLowerCase()}`}>
                    {n.name}
                  </a>
                </li>
              );
            })}
          </ul>
        </nav>
        <ThemeSelector />
      </div>
    </header>
  );
}

/**
 * Slim persistent bar, shown when an episode is loaded but its full player
 * isn't on screen (other routes, or an archive episode on the dashboard).
 */
export function NowPlayingBar({ hiddenForEpisodeId }: { hiddenForEpisodeId: string | null }) {
  const s = usePlayerState();
  const player = usePlayer();
  if (!s.episode || s.episode.episodeId === hiddenForEpisodeId) return null;
  const ep = s.episode;
  const pct = s.duration ? (s.currentTime / s.duration) * 100 : 0;
  return (
    <div className="nowbar" role="region" aria-label="Now playing" data-testid="bar-now-playing">
      <div className="nowbar-progress" style={{ width: `${pct}%` }} aria-hidden="true" />
      <div className="container nowbar-inner">
        <button
          type="button"
          className={`play-btn play-sm ${s.playing ? 'is-playing' : ''}`}
          onClick={() => player.toggle(ep)}
          aria-label={s.playing ? `Pause: ${ep.title}` : `Resume: ${ep.title}`}
          data-testid="button-nowbar-toggle"
        >
          {s.playing ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
        </button>
        <div className="nowbar-text">
          <span className="eyebrow">{s.playing ? 'Now playing' : 'Paused'}</span>
          <span className="nowbar-title">{ep.title}</span>
        </div>
        <span className="mono nowbar-time">
          {formatDuration(s.currentTime)} / {formatDuration(s.duration || ep.audioDurationSeconds)}
        </span>
      </div>
    </div>
  );
}

export function Footer() {
  const { scenario, setScenario } = useContent();
  return (
    <footer className="site-footer">
      <div className="container footer-inner">
        <div className="footer-col">
          <p className="footer-brand">Market Intelligence Podcast</p>
          <p className="faint">
            Prototype front end. All briefings, episodes and figures shown are sample content for design review. Automatic publishing from the AWS
            pipeline is not connected in this preview.
          </p>
        </div>
        <fieldset className="scenario" data-testid="scenario-switcher">
          <legend className="eyebrow">Prototype: content state</legend>
          <div className="scenario-opts">
            {SCENARIOS.map((s) => (
              <label key={s.id} className="chip-radio">
                <input
                  type="radio"
                  name="scenario"
                  value={s.id}
                  checked={scenario === s.id}
                  onChange={() => setScenario(s.id)}
                  data-testid={`radio-scenario-${s.id}`}
                />
                <span>{s.label}</span>
              </label>
            ))}
          </div>
          <p className="fine-print">Simulates manifest states for review. Audio playback is unaffected.</p>
        </fieldset>
      </div>
    </footer>
  );
}
