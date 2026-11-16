import { publishedBriefings, publishedEpisodes } from '../content/selectors';
import { formatShortDate } from '../lib/format';
import { href } from '../lib/router';
import { useBriefingBody, useContent } from '../state/content';
import { BriefingBlocks, SourceList } from '../components/BriefingBlocks';
import { BriefingHeader, Recap } from '../components/BriefingPanel';
import { EpisodePlayer } from '../components/EpisodePlayer';
import { ArrowLeft, ArrowRight } from '../components/Icons';
import { SkeletonLines, StateMessage } from '../components/ui';

export function ArticlePage({ editionId }: { editionId: string }) {
  const { state, retry } = useContent();
  const body = useBriefingBody(state.status === 'ready' ? editionId : undefined);

  const back = (
    <nav className="crumbs" aria-label="Breadcrumb">
      <a href={href.dashboard()} className="text-link" data-testid="link-back-dashboard">
        <ArrowLeft size={14} /> Dashboard
      </a>
      <span aria-hidden="true" className="faint">
        /
      </span>
      <a href={href.briefings()} className="text-link">
        Briefings
      </a>
    </nav>
  );

  if (state.status === 'loading')
    return (
      <div className="container page-pad article-page">
        {back}
        <div className="panel" aria-busy="true">
          <div className="skeleton skeleton-title" />
          <SkeletonLines lines={10} />
        </div>
      </div>
    );
  if (state.status === 'error')
    return (
      <div className="container page-pad">
        {back}
        <StateMessage
          tone="error"
          title="This briefing could not be loaded."
          action={
            <button type="button" className="btn btn-primary" onClick={retry}>
              Try again
            </button>
          }
        >
          <p>{state.message}</p>
        </StateMessage>
      </div>
    );

  const m = state.manifest;
  const tz = m.publication.timeZone;
  const all = publishedBriefings(m);
  const idx = all.findIndex((b) => b.editionId === editionId);
  const briefing = all[idx];
  if (!briefing)
    return (
      <div className="container page-pad">
        {back}
        <StateMessage
          title="Briefing not found."
          action={
            <a className="btn btn-secondary" href={href.briefings()}>
              Browse all briefings
            </a>
          }
        >
          <p>It may not be published yet, or the link may be incorrect.</p>
        </StateMessage>
      </div>
    );

  const newer = all[idx - 1];
  const older = all[idx + 1];
  const episode = publishedEpisodes(m).find((e) => e.editionId === editionId && e.hasAudio) ?? null;

  return (
    <div className="container page-pad article-page">
      {back}
      <div className="article-grid">
        <article className="panel article" aria-labelledby="article-title" data-testid="article-briefing">
          <header className="briefing-head">
            <BriefingHeader
              briefing={briefing}
              tz={tz}
              body={body.blocks}
              eyebrow={idx === 0 ? 'Latest briefing' : 'Briefing'}
              titleId="article-title"
              headingLevel={1}
            />
          </header>
          <Recap briefing={briefing} />
          <div className="article-body prose">
            {body.blocks ? (
              <>
                <BriefingBlocks blocks={body.blocks} idPrefix={`a-${briefing.editionId}`} />
                <SourceList sources={briefing.sources} isSample={briefing.isSample} />
              </>
            ) : body.error ? (
              <p className="inline-error" role="alert">
                The full text could not be loaded.{' '}
                <button type="button" className="link-btn" onClick={body.retry}>
                  Retry
                </button>
              </p>
            ) : (
              <SkeletonLines lines={10} />
            )}
          </div>
          <footer className="article-foot">
            <a className="btn btn-secondary" href={href.dashboard()}>
              <ArrowLeft size={14} /> Back to dashboard
            </a>
            <div className="pager">
              {newer && (
                <a className="text-link" href={href.briefing(newer.editionId)} data-testid="link-newer">
                  <ArrowLeft size={14} /> Newer · {formatShortDate(newer.publishedAt, tz)}
                </a>
              )}
              {older && (
                <a className="text-link" href={href.briefing(older.editionId)} data-testid="link-older">
                  Older · {formatShortDate(older.publishedAt, tz)} <ArrowRight size={14} />
                </a>
              )}
            </div>
          </footer>
        </article>
        <aside className="article-side" aria-label="Episode for this edition">
          <div className="side-stack">
            {episode ? (
              <EpisodePlayer episode={episode} tz={tz} label="Episode for this edition" />
            ) : (
              <section className="panel" aria-label="Episode for this edition">
                <p className="eyebrow">Episode for this edition</p>
                <p className="faint">No audio is available for this edition.</p>
                <a className="text-link" href={href.episodes()}>
                  Browse episodes <ArrowRight size={14} />
                </a>
              </section>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
