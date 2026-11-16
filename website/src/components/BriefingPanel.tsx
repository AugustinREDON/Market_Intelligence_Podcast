import { useLayoutEffect, useRef, useState } from 'react';
import type { BriefingBlock, BriefingItem, EpisodeItem } from '../content/types';
import { briefingWordCount, readingMinutes, blockWordCount } from '../content/selectors';
import { formatDate, formatDuration, formatNumber, formatTimeParts, isoDay } from '../lib/format';
import { href } from '../lib/router';
import { useBriefingBody } from '../state/content';
import { useEpisodeState, usePlayer } from '../state/player';
import { BriefingBlocks, SourceList } from './BriefingBlocks';
import { ArrowRight, ChevronDown, ChevronUp, PauseIcon, PlayIcon, Spinner } from './Icons';
import { PubTime, SampleNotice, SkeletonLines, Tag, TopicTags } from './ui';

/** Header shared by the dashboard panel and the article view. */
export function BriefingHeader({
  briefing,
  tz,
  body,
  eyebrow,
  titleId,
  headingLevel = 2,
}: {
  briefing: BriefingItem;
  tz: string;
  body: BriefingBlock[] | null;
  eyebrow: string;
  titleId: string;
  headingLevel?: 1 | 2;
}) {
  const words = briefingWordCount(briefing, body);
  const minutes = briefing.readingMinutes ?? readingMinutes(words);
  const H = headingLevel === 1 ? 'h1' : 'h2';
  return (
    <>
      <div className="briefing-eyebrow-row">
        <span className="eyebrow">{eyebrow}</span>
        {briefing.isSample && <Tag tone="sample">Sample edition</Tag>}
        <TopicTags topics={briefing.topics} max={5} />
      </div>
      <H className="briefing-title" id={titleId}>
        {briefing.title}
      </H>
      <dl className="meta-row">
        <div>
          <dt>Published</dt>
          <dd>
            <PubTime iso={briefing.publishedAt} tz={tz} withTime />
          </dd>
        </div>
        <div>
          <dt>Reading time</dt>
          <dd className="mono">{body || briefing.readingMinutes ? `${minutes} min read` : '…'}</dd>
        </div>
        {body && (
          <div className="hide-sm">
            <dt>Length</dt>
            <dd className="mono">{formatNumber(words)} words</dd>
          </div>
        )}
      </dl>
      {briefing.isSample && (
        <SampleNotice>
          Demonstration content written to test the layout at real article length. It is not current reporting, and all figures are illustrative.
        </SampleNotice>
      )}
    </>
  );
}

export function Recap({ briefing }: { briefing: BriefingItem }) {
  return (
    <div className="recap prose">
      <p className="lede">{briefing.summary}</p>
      {briefing.takeaways.length > 0 && (
        <>
          <h3 className="recap-h">Main takeaways</h3>
          <ul className="takeaways">
            {briefing.takeaways.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function ListenButton({ episode, compact = false }: { episode: EpisodeItem; compact?: boolean }) {
  const player = usePlayer();
  const st = useEpisodeState(episode);
  return (
    <button
      type="button"
      className="btn btn-ghost"
      onClick={() => player.toggle(episode)}
      aria-label={st.playing ? `Pause episode: ${episode.title}` : `Listen to this edition's episode: ${episode.title}`}
      data-testid="button-listen-edition"
    >
      {st.buffering ? <Spinner /> : st.playing ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
      <span>{st.playing ? 'Pause episode' : compact ? 'Listen' : 'Listen to this edition'}</span>
      <span className="mono faint">{formatDuration(episode.audioDurationSeconds)}</span>
    </button>
  );
}

/** "Today's briefing" only when the edition really is from today (publication time zone). */
function briefingLabel(briefing: BriefingItem, tz: string) {
  return isoDay(briefing.publishedAt, tz) === isoDay(new Date().toISOString(), tz) ? 'Today\u2019s briefing' : 'Latest briefing';
}

/** Single plain-text metadata line for the refined (Forest homepage) panel. */
function RefinedMeta({ briefing, tz, minutes }: { briefing: BriefingItem; tz: string; minutes: number | null }) {
  const { time, zone, offset } = formatTimeParts(briefing.publishedAt, tz);
  return (
    <p className="hb-meta">
      <time dateTime={briefing.publishedAt} title={`${tz.replace(/_/g, ' ')} time`}>
        {formatDate(briefing.publishedAt, tz)}, {time} {zone}
        {offset && <span className="hb-meta-faint"> ({offset})</span>}
      </time>
      {minutes != null && (
        <>
          <span className="hb-sep" aria-hidden="true">
            ·
          </span>
          <span>{minutes} min read</span>
        </>
      )}
    </p>
  );
}

export function BriefingPanel({
  briefing,
  tz,
  episode,
  refined = false,
}: {
  briefing: BriefingItem;
  tz: string;
  episode: EpisodeItem | null;
  /** Forest homepage presentation: shorter collapsed view, details moved into the expanded region. */
  refined?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const { blocks, error, loading, retry } = useBriefingBody(briefing.editionId);
  const headRef = useRef<HTMLElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const pendingReturn = useRef(false);
  const regionId = `briefing-full-${briefing.editionId}`;

  const sections = blocks?.filter((b) => b.type === 'heading').length ?? 0;
  const remainingMin = blocks ? readingMinutes(blockWordCount(blocks)) : null;

  const collapse = () => {
    pendingReturn.current = true;
    setExpanded(false);
  };

  // After collapsing, bring the reader back to the briefing header (only if it
  // has scrolled out of view) and return focus to the disclosure control.
  useLayoutEffect(() => {
    if (expanded || !pendingReturn.current) return;
    pendingReturn.current = false;
    const head = headRef.current;
    if (head) {
      const top = head.getBoundingClientRect().top;
      if (top < 0 || top > window.innerHeight * 0.6) head.scrollIntoView({ block: 'start', behavior: 'auto' });
    }
    toggleRef.current?.focus({ preventScroll: true });
  }, [expanded]);

  return (
    <article className={`panel briefing ${refined ? "is-refined" : ""}`} aria-labelledby="latest-briefing-title" data-testid="panel-latest-briefing">
      {refined ? (
        <>
          <header className="briefing-head hb-head scroll-target" ref={headRef}>
            <p className="hb-label">{briefingLabel(briefing, tz)}</p>
            <h2 className="hb-title" id="latest-briefing-title">
              {briefing.title}
            </h2>
            <RefinedMeta briefing={briefing} tz={tz} minutes={briefing.readingMinutes ?? (blocks ? readingMinutes(briefingWordCount(briefing, blocks)) : null)} />
          </header>
          <p className="hb-recap">{briefing.recap || briefing.summary}</p>
        </>
      ) : (
        <>
          <header className="briefing-head scroll-target" ref={headRef}>
            <BriefingHeader briefing={briefing} tz={tz} body={blocks} eyebrow="Latest briefing" titleId="latest-briefing-title" />
          </header>
          <Recap briefing={briefing} />
        </>
      )}

      <div className="disclosure-bar">
        <button
          ref={toggleRef}
          type="button"
          className="btn btn-primary btn-disclosure"
          aria-expanded={expanded ? 'true' : 'false'}
          aria-controls={regionId}
          disabled={!blocks}
          onClick={() => (expanded ? collapse() : setExpanded(true))}
          data-testid="button-toggle-briefing"
        >
          {expanded ? <ChevronUp /> : <ChevronDown />}
          <span>{expanded ? 'Collapse briefing' : 'Expand full briefing'}</span>
          {blocks && !expanded && !refined && (
            <span className="btn-meta mono">
              {sections} sections · {remainingMin} min
            </span>
          )}
          {loading && <Spinner />}
        </button>
        {episode && !refined && <ListenButton episode={episode} />}
      </div>
      {error && (
        <p className="inline-error" role="alert">
          The full briefing could not be loaded.{' '}
          <button type="button" className="link-btn" onClick={retry}>
            Retry
          </button>
        </p>
      )}

      <div id={regionId} className="briefing-full" hidden={!expanded} data-testid="region-briefing-full">
        {blocks && refined && (
          <div className="hb-extra">
            {briefing.takeaways.length > 0 && (
              <section aria-labelledby={`${regionId}-tk`}>
                <h3 className="hb-subhead" id={`${regionId}-tk`}>
                  Main takeaways
                </h3>
                <ul className="hb-takeaways">
                  {briefing.takeaways.map((t, i) => (
                    <li key={i}>{t}</li>
                  ))}
                </ul>
              </section>
            )}
            <dl className="hb-facts">
              {briefing.topics.length > 0 && (
                <div>
                  <dt>Topics</dt>
                  <dd>{briefing.topics.join(' · ')}</dd>
                </div>
              )}
              <div>
                <dt>Length</dt>
                <dd>
                  {formatNumber(briefingWordCount(briefing, blocks))} words · {blocks.filter((b) => b.type === 'heading').length} sections
                </dd>
              </div>
            </dl>
          </div>
        )}
        {blocks ? (
          <div className="prose">
            <BriefingBlocks blocks={blocks} idPrefix={briefing.editionId} />
            <SourceList sources={briefing.sources} isSample={briefing.isSample} />
          </div>
        ) : (
          <SkeletonLines lines={8} />
        )}
        <div className="disclosure-bar disclosure-bottom">
          <button
            type="button"
            className="btn btn-secondary"
            aria-expanded={expanded ? 'true' : 'false'}
            aria-controls={regionId}
            onClick={collapse}
            data-testid="button-collapse-briefing-bottom"
          >
            <ChevronUp />
            <span>Collapse briefing</span>
          </button>
          <a className="text-link" href={href.briefing(briefing.editionId)}>
            Open in article view <ArrowRight size={14} />
          </a>
        </div>
      </div>
    </article>
  );
}
