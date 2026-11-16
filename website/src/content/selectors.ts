/**
 * Pure functions that turn a manifest into what the UI shows.
 * Only `published` content whose publication time has passed is ever returned.
 */
import type { BriefingBlock, BriefingItem, EpisodeItem, Manifest } from './types';

const ts = (iso: string) => {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
};

export function publishedBriefings(m: Manifest, now = Date.now()): BriefingItem[] {
  return m.editions
    .filter((e) => e.briefing && e.briefing.status === 'published' && ts(e.briefing.publishedAt) <= now)
    .map((e) => ({ ...e.briefing!, editionId: e.editionId, topics: e.topics, isSample: !!m.isSampleContent }))
    .sort((a, b) => ts(b.publishedAt) - ts(a.publishedAt));
}

export function publishedEpisodes(m: Manifest, now = Date.now()): EpisodeItem[] {
  return m.editions
    .filter(
      (e) => e.episode && e.episode.status === 'published' && e.episode.publishedAt && ts(e.episode.publishedAt) <= now,
    )
    .map((e) => ({
      ...e.episode!,
      publishedAt: e.episode!.publishedAt!,
      editionId: e.editionId,
      topics: e.topics,
      isSample: !!m.isSampleContent,
      hasAudio: !!e.episode!.audioUrl,
    }))
    .sort((a, b) => ts(b.publishedAt) - ts(a.publishedAt));
}

export interface DailyState {
  latestBriefing: BriefingItem | null;
  /** Most recent published episode that has a playable file. */
  latestEpisode: EpisodeItem | null;
  /** True when latestEpisode belongs to the same edition as latestBriefing. */
  episodeMatchesBriefing: boolean;
  /** Status of the latest briefing's own episode, when it is not yet available. */
  pendingEpisodeStatus: 'processing' | 'failed' | 'missing' | 'no-audio' | null;
}

export function dailyState(m: Manifest, now = Date.now()): DailyState {
  const briefings = publishedBriefings(m, now);
  const episodes = publishedEpisodes(m, now);
  const latestBriefing = briefings[0] ?? null;
  const latestEpisode = episodes.find((e) => e.hasAudio) ?? null;
  const episodeMatchesBriefing = !!latestBriefing && !!latestEpisode && latestEpisode.editionId === latestBriefing.editionId;

  let pendingEpisodeStatus: DailyState['pendingEpisodeStatus'] = null;
  if (latestBriefing && !episodeMatchesBriefing) {
    const own = m.editions.find((e) => e.editionId === latestBriefing.editionId)?.episode;
    if (!own) pendingEpisodeStatus = 'missing';
    else if (own.status === 'failed') pendingEpisodeStatus = 'failed';
    else if (own.status === 'published' && !own.audioUrl) pendingEpisodeStatus = 'no-audio';
    else pendingEpisodeStatus = 'processing';
  }
  return { latestBriefing, latestEpisode, episodeMatchesBriefing, pendingEpisodeStatus };
}

/* ---------- Reading time ---------- */

const stripInline = (s: string) => s.replace(/\*\*/g, '').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
const words = (s: string) => stripInline(s).trim().split(/\s+/).filter(Boolean).length;

export function blockWordCount(blocks: BriefingBlock[]): number {
  let n = 0;
  for (const b of blocks) {
    if (b.type === 'list') b.items.forEach((i) => (n += words(i)));
    else if (b.type === 'table') n += words(b.caption ?? '') + b.rows.flat().length;
    else n += words(b.text);
  }
  return n;
}

export function briefingWordCount(b: BriefingItem, body: BriefingBlock[] | null): number {
  if (b.wordCount) return b.wordCount;
  return words(b.title) + words(b.summary) + b.takeaways.reduce((s, t) => s + words(t), 0) + (body ? blockWordCount(body) : 0);
}

export const WORDS_PER_MINUTE = 230;
export const readingMinutes = (wordCount: number) => Math.max(1, Math.round(wordCount / WORDS_PER_MINUTE));

/* ---------- Archive search ---------- */

export type SortOrder = 'newest' | 'oldest';

export function searchItems<T extends { title: string; publishedAt: string; topics: string[] }>(
  items: T[],
  query: string,
  order: SortOrder,
  extraText: (item: T) => string = () => '',
): T[] {
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const filtered = terms.length
    ? items.filter((it) => {
        const hay = [it.title, it.topics.join(' '), extraText(it), it.publishedAt.slice(0, 10), searchableDate(it.publishedAt)].join(' ').toLowerCase();
        return terms.every((t) => hay.includes(t));
      })
    : items.slice();
  filtered.sort((a, b) => (order === 'newest' ? ts(b.publishedAt) - ts(a.publishedAt) : ts(a.publishedAt) - ts(b.publishedAt)));
  return filtered;
}

/** "13 november 2026 nov friday fri" so archive search accepts natural dates. */
function searchableDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const long = d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
  const short = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  return `${long} ${short}`.replace(/,/g, '');
}
