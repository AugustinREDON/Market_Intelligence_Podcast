import type { BriefingItem, EpisodeItem } from '../content/types';
import { formatDate, formatDuration } from '../lib/format';
import { href } from '../lib/router';
import { useEpisodeState } from '../state/player';
import { DownloadLink, PlayButton } from './EpisodePlayer';
import { ArrowRight, DownloadIcon } from './Icons';
import { PubTime, Tag, TopicTags } from './ui';

export function EpisodeRow({
  episode,
  tz,
  showDescription = true,
  simple = false,
}: {
  episode: EpisodeItem;
  tz: string;
  showDescription?: boolean;
  /** Homepage (Forest) presentation: no badges or topic tags, one-line description. */
  simple?: boolean;
}) {
  const st = useEpisodeState(episode);
  return (
    <li className={`row episode-row ${st.active ? 'is-active' : ''} ${simple ? 'is-simple' : ''}`} data-testid={`row-episode-${episode.episodeId}`}>
      <div className="row-lead">
        <PlayButton episode={episode} size="sm" />
      </div>
      <div className="row-main">
        <p className="row-meta">
          <PubTime iso={episode.publishedAt} tz={tz} />
          <span className="sep" aria-hidden="true">
            ·
          </span>
          {episode.hasAudio ? (
            <span className="mono">
              <span className="sr-only">Duration </span>
              {formatDuration(episode.audioDurationSeconds)}
            </span>
          ) : (
            <span className="faint">Audio not available</span>
          )}
          {episode.isDemoAudio && !simple && <Tag tone="sample">Demo audio</Tag>}
          {st.playing && <span className="now-tag">Playing</span>}
        </p>
        <h3 className="row-title">{episode.title}</h3>
        {showDescription && <p className="row-desc">{episode.description}</p>}
        {!simple && <TopicTags topics={episode.topics} max={3} />}
      </div>
      <div className="row-trail">
        {episode.hasAudio ? (
          <DownloadLink episode={episode} label="MP3" className="btn btn-ghost btn-sm" />
        ) : (
          <span className="btn btn-ghost btn-sm is-disabled" aria-hidden="true" title="No audio file for this demo entry">
            <DownloadIcon size={15} />
            <span>MP3</span>
          </span>
        )}
      </div>
    </li>
  );
}

export function BriefingRow({ briefing, tz, simple = false }: { briefing: BriefingItem; tz: string; simple?: boolean }) {
  return (
    <li className={`row briefing-row ${simple ? 'is-simple' : ''}`} data-testid={`row-briefing-${briefing.editionId}`}>
      <div className="row-main">
        <p className="row-meta">
          <PubTime iso={briefing.publishedAt} tz={tz} />
          {briefing.isSample && !simple && <Tag tone="sample">Sample</Tag>}
        </p>
        <h3 className="row-title">
          <a href={href.briefing(briefing.editionId)} className="row-link stretched" data-testid={`link-read-${briefing.editionId}`}>
            {briefing.title}
          </a>
        </h3>
        <p className="row-desc">{briefing.summary}</p>
        {!simple && <TopicTags topics={briefing.topics} max={3} />}
      </div>
      <div className="row-trail">
        {/* Visual affordance only: the title link covers the whole row. */}
        <span className="btn btn-ghost btn-sm row-read" aria-hidden="true">
          <span>Read</span>
          <ArrowRight size={14} />
        </span>
      </div>
    </li>
  );
}

export function EditionRow({
  editionId,
  briefing,
  episode,
  tz,
}: {
  editionId: string;
  briefing: BriefingItem | null;
  episode: EpisodeItem | null;
  tz: string;
}) {
  const st = useEpisodeState(episode);
  const content = briefing ?? episode;

  if (!content) return null;

  const playLabel = st.playing
    ? 'Pause'
    : st.active && st.currentTime > 0
      ? 'Resume'
      : 'Play';

  return (
    <li className="edition-row">
      <div className="edition-copy">
        <p className="edition-date">
          <time dateTime={content.publishedAt}>
            {formatDate(content.publishedAt, tz)}
          </time>
        </p>

        <h3 className="edition-title">
          {briefing ? (
            <a href={href.briefing(editionId)}>{briefing.title}</a>
          ) : (
            episode?.title
          )}
        </h3>

        <p className="edition-summary">
          {briefing?.summary ?? episode?.description}
        </p>
      </div>

      <div className="edition-actions">
        {briefing && (
          <a
            className="btn btn-secondary"
            href={href.briefing(editionId)}
            aria-label={`Read: ${briefing.title}`}
          >
            Read <ArrowRight size={14} />
          </a>
        )}

        {episode?.hasAudio ? (
          <div className="edition-listen">
            <PlayButton episode={episode} size="sm" />

            <div className="edition-play-text">
              <span className="edition-play-label" aria-hidden="true">
                {playLabel}
              </span>

              {episode.audioDurationSeconds != null && (
                <span className="edition-duration">
                  <span className="sr-only">Duration </span>
                  {formatDuration(episode.audioDurationSeconds)}
                </span>
              )}
            </div>
          </div>
        ) : (
          <span className="edition-unavailable">Audio unavailable</span>
        )}

        {st.error && (
          <p className="edition-error inline-error" role="alert">
            {st.error}
          </p>
        )}
      </div>
    </li>
  );
}