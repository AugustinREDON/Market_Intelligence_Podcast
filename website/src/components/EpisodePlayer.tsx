import { useId, type ReactNode } from 'react';
import type { EpisodeItem } from '../content/types';
import { formatBytes, formatDate, formatDuration, spokenDuration } from '../lib/format';
import { PLAYBACK_RATES, useEpisodeState, usePlayer } from '../state/player';
import { Back10, ChevronDown, DownloadIcon, Fwd30, MuteIcon, PauseIcon, PlayIcon, Spinner, VolumeIcon } from './Icons';
import { PubTime, Tag } from './ui';

export const downloadName = (ep: EpisodeItem) => `market-intelligence-${ep.editionId}${ep.isDemoAudio ? '-demo' : ''}.mp3`;

export function DownloadLink({ episode, label = 'Download MP3', className = 'btn btn-secondary' }: { episode: EpisodeItem; label?: string; className?: string }) {
  const { resolveUrl } = usePlayer();
  if (!episode.audioUrl) return null;
  const size = formatBytes(episode.audioBytes);
  return (
    <a
      className={className}
      href={resolveUrl(episode.audioUrl)}
      download={downloadName(episode)}
      type={episode.audioMimeType ?? 'audio/mpeg'}
      aria-label={`${label}: ${episode.title}${size ? `, ${size}` : ''}`}
      data-testid={`link-download-${episode.episodeId}`}
    >
      <DownloadIcon size={15} />
      <span>{label}</span>
      {size && <span className="mono faint btn-meta">{size}</span>}
    </a>
  );
}

export function PlayButton({ episode, size = 'md' }: { episode: EpisodeItem; size?: 'sm' | 'md' | 'lg' }) {
  const player = usePlayer();
  const st = useEpisodeState(episode);
  if (!episode.hasAudio) {
    return (
      <span className={`play-btn play-${size} is-unavailable`} role="img" aria-label="Audio not available">
        <PlayIcon size={size === 'lg' ? 20 : 14} />
      </span>
    );
  }
  const label = st.playing ? `Pause: ${episode.title}` : `${st.active && st.currentTime > 0 ? 'Resume' : 'Play'}: ${episode.title}`;
  return (
    <button
      type="button"
      className={`play-btn play-${size} ${st.playing ? 'is-playing' : ''}`}
      onClick={() => player.toggle(episode)}
      aria-label={label}
      aria-pressed={st.playing ? 'true' : 'false'}
      data-testid={`button-play-${episode.episodeId}`}
    >
      {st.buffering ? <Spinner size={size === 'lg' ? 20 : 14} /> : st.playing ? <PauseIcon size={size === 'lg' ? 20 : 14} /> : <PlayIcon size={size === 'lg' ? 20 : 14} />}
    </button>
  );
}

export function Transport({ episode, compact = false, refined = false }: { episode: EpisodeItem; compact?: boolean; refined?: boolean }) {
  const player = usePlayer();
  const st = useEpisodeState(episode);
  const id = useId();
  const dur = st.duration || episode.audioDurationSeconds || 0;
  const pct = dur ? Math.min(100, (st.currentTime / dur) * 100) : 0;

  return (
    <div className={`transport ${compact ? 'is-compact' : ''}`}>
      <div className="transport-row">
        {!compact && (
          <button type="button" className="icon-btn" onClick={() => player.skip(-10)} disabled={!st.active} aria-label="Back 10 seconds">
            <Back10 size={20} />
          </button>
        )}
        <PlayButton episode={episode} size={compact ? 'md' : 'lg'} />
        {!compact && (
          <button type="button" className="icon-btn" onClick={() => player.skip(30)} disabled={!st.active} aria-label="Forward 30 seconds">
            <Fwd30 size={20} />
          </button>
        )}
        <div className="seek">
          <label htmlFor={`${id}-seek`} className="sr-only">
            Seek
          </label>
          <input
            id={`${id}-seek`}
            type="range"
            className="range"
            min={0}
            max={Math.max(1, Math.floor(dur))}
            step={1}
            value={Math.floor(st.currentTime)}
            style={{ ['--pct' as string]: `${pct}%` }}
            onChange={(e) => player.seek(episode, Number(e.currentTarget.value))}
            aria-valuetext={`${spokenDuration(st.currentTime)} of ${spokenDuration(dur)}`}
            data-testid="input-seek"
          />
          <div className="seek-times mono" aria-hidden="true">
            <span data-testid="text-elapsed">{formatDuration(st.currentTime)}</span>
            <span>{formatDuration(dur)}</span>
          </div>
        </div>
      </div>

      <div className="transport-row transport-secondary">
        <label className="speed">
          <span className="sr-only">Playback speed</span>
          <span aria-hidden="true" className="faint">
            Speed
          </span>
          <select
            className="select select-sm mono"
            value={String(st.rate)}
            onChange={(e) => player.setRate(Number(e.currentTarget.value))}
            data-testid="select-speed"
          >
            {PLAYBACK_RATES.map((r) => (
              <option key={r} value={String(r)}>
                {r}×
              </option>
            ))}
          </select>
        </label>
        {player.volumeSupported && !compact && (
          <div className="volume">
            <button
              type="button"
              className="icon-btn icon-btn-sm"
              onClick={player.toggleMute}
              aria-label={st.muted ? 'Unmute' : 'Mute'}
              aria-pressed={st.muted ? 'true' : 'false'}
              data-testid="button-mute"
            >
              {st.muted || st.volume === 0 ? <MuteIcon size={16} /> : <VolumeIcon size={16} />}
            </button>
            <label htmlFor={`${id}-vol`} className="sr-only">
              Volume
            </label>
            <input
              id={`${id}-vol`}
              type="range"
              className="range range-sm"
              min={0}
              max={1}
              step={0.05}
              value={st.muted ? 0 : st.volume}
              style={{ ['--pct' as string]: `${(st.muted ? 0 : st.volume) * 100}%` }}
              onChange={(e) => player.setVolume(Number(e.currentTarget.value))}
              aria-valuetext={`${Math.round((st.muted ? 0 : st.volume) * 100)}%`}
              data-testid="input-volume"
            />
          </div>
        )}
        {compact && (
          <DownloadLink episode={episode} className="btn btn-ghost btn-sm" label={refined ? 'Download MP3' : 'MP3'} />
        )}
      </div>
      {st.error && (
        <p className="inline-error" role="alert">
          {st.error}
        </p>
      )}
    </div>
  );
}

export function EpisodePlayer({
  episode,
  tz,
  label,
  note,
  variant = 'full',
  refined = false,
}: {
  episode: EpisodeItem | null;
  tz: string;
  label: string;
  note?: ReactNode;
  variant?: 'full' | 'compact';
  /** Forest homepage presentation: lighter player, description and hosts in a disclosure. */
  refined?: boolean;
}) {
  const titleId = useId();
  const compact = variant === 'compact';
  const refinedCls = refined ? 'is-refined' : '';
  if (!episode) {
    return (
      <section className={`panel player ${compact ? 'player-compact' : ''} ${refinedCls}`} aria-labelledby={titleId} data-testid={`panel-player-${variant}`}>
        <h2 className="eyebrow" id={titleId}>
          {label}
        </h2>
        <p className="state-title">No episode is available yet.</p>
        {note}
      </section>
    );
  }
  return (
    <section
      className={`panel player ${compact ? 'player-compact' : ''} ${refinedCls}`}
      aria-labelledby={titleId}
      data-testid={`panel-player-${variant}`}
    >
{refined ? (
<>
      <p className="player-label">{label}</p>
      <h2 className="player-title" id={titleId}>
        {episode.title}
      </h2>
      <p className="player-meta">
        <time dateTime={episode.publishedAt}>{formatDate(episode.publishedAt, tz)}</time>
        <span className="sep" aria-hidden="true">
          ·
        </span>
        <span>
          <span className="sr-only">Duration </span>
          {formatDuration(episode.audioDurationSeconds)}
        </span>
      </p>

      <Transport episode={episode} compact={compact} refined />

      {!compact && (
        <div className="player-actions">
          <DownloadLink episode={episode} />
        </div>
      )}
      <details className="ep-details">
        <summary data-testid="toggle-episode-details">
          <span>Episode details</span>
          <ChevronDown size={16} />
        </summary>
        <div className="ep-details-body">
          <p>{episode.description}</p>
          <p className="ep-hosts">
            <span>Hosts</span> {episode.hosts.join(' & ')}
          </p>
        </div>
      </details>
</>
) : (
<>
      <div className="player-eyebrow-row">
        <span className="eyebrow">{label}</span>
        {episode.isDemoAudio && <Tag tone="sample">Demo audio</Tag>}
      </div>
      <h2 className="player-title" id={titleId}>
        {episode.title}
      </h2>
      <p className="player-meta">
        <PubTime iso={episode.publishedAt} tz={tz} />
        <span className="sep" aria-hidden="true">
          ·
        </span>
        <span className="mono">
          <span className="sr-only">Duration </span>
          {formatDuration(episode.audioDurationSeconds)}
        </span>
      </p>
      {!compact && <p className="player-desc">{episode.description}</p>}
      <p className="hosts">
        <span className="faint">Hosts</span> {episode.hosts.join(' & ')}
      </p>

      <Transport episode={episode} compact={compact} />

      {!compact && (
        <div className="player-actions">
          <DownloadLink episode={episode} />
        </div>
      )}
      {episode.isDemoAudio && !compact && (
        <p className="fine-print">Demo audio: synthetic voices reading a short sample script. Not a real broadcast.</p>
      )}
</>
)}
      {note}
    </section>
  );
}
