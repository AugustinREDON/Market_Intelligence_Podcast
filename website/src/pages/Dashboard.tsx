import type { Manifest } from '../content/types';
import { dailyState, publishedBriefings, publishedEpisodes } from '../content/selectors';
import { formatLongDate, formatShortDate, formatTimeParts } from '../lib/format';
import { href } from '../lib/router';
import { useContent } from '../state/content';
import { BriefingPanel } from '../components/BriefingPanel';
import { EpisodePlayer } from '../components/EpisodePlayer';
import { ArrowRight } from '../components/Icons';
import { BriefingRow, EpisodeRow } from '../components/Lists';
import { Panel, SkeletonLines, StateMessage } from '../components/ui';
import { sidebarModules } from './sidebarModules';
import { useTheme } from '../state/theme';

function StatusItem({ label, state, children }: { label: string; state: 'ok' | 'pending' | 'none' | 'sample'; children: React.ReactNode }) {
  return (
    <div className="status-item">
      <dt>{label}</dt>
      <dd>
        <span className={`dot dot-${state}`} aria-hidden="true" />
        {children}
      </dd>
    </div>
  );
}

function DailyHeader({ m }: { m: Manifest }) {
  const tz = m.publication.timeZone;
  const d = dailyState(m);
  const b = d.latestBriefing;
  const t = (iso: string) => {
    const p = formatTimeParts(iso, tz);
    return `${p.time} ${p.zone}`;
  };
  return (
    <section className="daily" aria-labelledby="daily-title">
      <div className="container daily-inner">
        <div className="daily-main">
          <p className="eyebrow">Latest edition</p>
          <h1 className="daily-date" id="daily-title">
            {b ? (
              <time dateTime={b.publishedAt.slice(0, 10)}>{formatLongDate(b.publishedAt, tz)}</time>
            ) : (
              'No edition published yet'
            )}
          </h1>
          <p className="daily-desc">
            {m.publication.description} Hosted by {m.publication.hosts.join(' and ')}.
          </p>
        </div>
        <dl className="status-list" aria-label="Publication status">
          <StatusItem label="Briefing" state={b ? 'ok' : 'none'}>
            {b ? <span className="mono">Published {t(b.publishedAt)}</span> : 'Not yet published'}
          </StatusItem>
          <StatusItem label="Episode" state={d.episodeMatchesBriefing ? 'ok' : d.pendingEpisodeStatus === 'processing' ? 'pending' : 'none'}>
            {d.episodeMatchesBriefing && d.latestEpisode ? (
              <span className="mono">Published {t(d.latestEpisode.publishedAt)}</span>
            ) : d.pendingEpisodeStatus === 'processing' ? (
              'In production'
            ) : d.pendingEpisodeStatus === 'failed' ? (
              'Delayed'
            ) : b ? (
              'No episode for this edition'
            ) : (
              '—'
            )}
          </StatusItem>
          {m.isSampleContent && (
            <StatusItem label="Content" state="sample">
              Sample data · not live
            </StatusItem>
          )}
        </dl>
      </div>
    </section>
  );
}

/** Homepage header: label, edition date, one sentence, one consolidated sample note. */
function RefinedDailyHeader({ m }: { m: Manifest }) {
  const tz = m.publication.timeZone;
  const briefing = dailyState(m).latestBriefing;

  // Change to 'left' to try the other arrangement.
  const datePosition: 'right' | 'left' = 'left';

  return (
    <section
      className={`daily daily-refined date-${datePosition}`}
      aria-labelledby="daily-title"
    >
      <div className="container daily-inner">
        <div className="header-message">
          <h1 className="header-tagline" id="daily-title">
            Go make better <span>small talk.</span>
          </h1>
          <p className="daily-desc">
            Your daily market briefing &amp; podcast
          </p>
        </div>

        <div className="header-edition">
          <p className="header-edition-label">Latest edition</p>
          <p className="header-edition-date">
            {briefing ? (
              <time dateTime={briefing.publishedAt}>
                {formatLongDate(briefing.publishedAt, tz)}
              </time>
            ) : (
              'Coming soon'
            )}
          </p>
        </div>

        {m.isSampleContent && (
          <p className="sr-only">
            Demonstration content, not live reporting.
          </p>
        )}
      </div>
    </section>
  );
}

function DashboardSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading latest edition">
      <section className="daily">
        <div className="container daily-inner">
          <div className="daily-main">
            <SkeletonLines lines={3} />
          </div>
        </div>
      </section>
      <div className="container dash-grid">
        <div className="main-col">
          <div className="panel">
            <p className="eyebrow">Latest briefing</p>
            <div className="skeleton skeleton-title" />
            <SkeletonLines lines={7} />
          </div>
        </div>
        <aside className="side-col">
          <div className="panel">
            <p className="eyebrow">Latest episode</p>
            <SkeletonLines lines={5} />
          </div>
        </aside>
      </div>
      <p className="sr-only" role="status">
        Loading the latest edition…
      </p>
    </div>
  );
}

export function Dashboard() {
  const { state, retry } = useContent();
  // The Forest theme has its own light homepage presentation; Blue keeps the original.
  const refined = useTheme().theme === 'mip';
  const Header = refined ? RefinedDailyHeader : DailyHeader;
  if (state.status === 'loading') return <DashboardSkeleton />;
  if (state.status === 'error')
    return (
      <div className="container page-pad">
        <StateMessage
          tone="error"
          title="The latest edition could not be loaded."
          action={
            <button type="button" className="btn btn-primary" onClick={retry} data-testid="button-retry">
              Try again
            </button>
          }
        >
          <p>
            The content manifest could not be read. If this persists, the publishing pipeline may not have written a manifest yet.{' '}
            <span className="mono faint">{state.message}</span>
          </p>
        </StateMessage>
      </div>
    );

  const m = state.manifest;
  const tz = m.publication.timeZone;
  const d = dailyState(m);
  const episodes = publishedEpisodes(m);
  const briefings = publishedBriefings(m);

  if (!d.latestBriefing && !d.latestEpisode) {
    return (
      <>
        <Header m={m} />
        <div className="container page-pad">
          <StateMessage title="Nothing has been published yet.">
            <p>The first briefing and episode will appear here as soon as they are marked as published.</p>
          </StateMessage>
        </div>
      </>
    );
  }

  const playerLabel = d.episodeMatchesBriefing || !d.latestBriefing ? 'Latest episode' : 'Latest available episode';
  const pendingNote =
    d.latestBriefing && !d.episodeMatchesBriefing ? (
      <p className="pending-note" role="note" data-testid="text-episode-pending">
        <span className={`dot dot-${d.pendingEpisodeStatus === 'processing' ? 'pending' : 'none'}`} aria-hidden="true" />
        {d.pendingEpisodeStatus === 'processing'
          ? `The episode for the ${formatShortDate(d.latestBriefing.publishedAt, tz)} briefing is still in production.`
          : d.pendingEpisodeStatus === 'failed'
            ? `The episode for the ${formatShortDate(d.latestBriefing.publishedAt, tz)} briefing is delayed.`
            : `There is no episode for the ${formatShortDate(d.latestBriefing.publishedAt, tz)} briefing.`}
        {d.latestEpisode && ` Showing the most recent episode, from ${formatShortDate(d.latestEpisode.publishedAt, tz)}.`}
      </p>
    ) : null;

  const companion = d.episodeMatchesBriefing ? d.latestEpisode : null;
  const recentEpisodes = episodes.filter((e) => e.episodeId !== d.latestEpisode?.episodeId).slice(0, 4);
  const recentBriefings = briefings.filter((b) => b.editionId !== d.latestBriefing?.editionId).slice(0, 4);

  return (
    <>
      <Header m={m} />
      <div className="container dash-grid">
        <div className="main-col">
          {/* Mobile/tablet: compact player first, so listening is one tap away. */}
          <div className="only-compact">
            <EpisodePlayer episode={d.latestEpisode} tz={tz} label={playerLabel} note={pendingNote} variant="compact" refined={refined} />
          </div>
          {d.latestBriefing ? (
            <BriefingPanel briefing={d.latestBriefing} tz={tz} episode={companion} refined={refined} />
          ) : (
            <Panel label="Latest briefing">
              <StateMessage title="No briefing has been published yet." />
            </Panel>
          )}
        </div>
        <aside className="side-col only-wide" aria-label="Latest episode and tools">
          <div className="side-stack">
            <EpisodePlayer episode={d.latestEpisode} tz={tz} label={playerLabel} note={pendingNote} refined={refined} />
            {sidebarModules.map((Mod, i) => (
              <Mod key={i} manifest={m} />
            ))}
          </div>
        </aside>
      </div>

      <section className="container recent" aria-labelledby="recent-title">
        <div className="section-head">
          <h2 className="section-title" id="recent-title">
            Recent editions
          </h2>
          {!refined && <p className="faint section-sub">Previous briefings and episodes, newest first.</p>}
        </div>
        <div className="recent-grid">
          <Panel
            label="Recent episodes"
            labelId="recent-episodes"
            actions={
              <a className="text-link" href={href.episodes()} data-testid="link-all-episodes">
                All episodes <ArrowRight size={14} />
              </a>
            }
          >
            {recentEpisodes.length ? (
              <ul className="rows">
                {recentEpisodes.map((e) => (
                  <EpisodeRow key={e.episodeId} episode={e} tz={tz} simple={refined} />
                ))}
              </ul>
            ) : (
              <p className="faint">No earlier episodes.</p>
            )}
          </Panel>
          <Panel
            label="Previous briefings"
            labelId="previous-briefings"
            actions={
              <a className="text-link" href={href.briefings()} data-testid="link-all-briefings">
                All briefings <ArrowRight size={14} />
              </a>
            }
          >
            {recentBriefings.length ? (
              <ul className="rows">
                {recentBriefings.map((b) => (
                  <BriefingRow key={b.editionId} briefing={b} tz={tz} simple={refined} />
                ))}
              </ul>
            ) : (
              <p className="faint">No earlier briefings.</p>
            )}
          </Panel>
        </div>
      </section>
    </>
  );
}
