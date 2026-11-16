/**
 * Content-loading interface.
 *
 * Everything the UI knows about briefings and episodes comes through a
 * `ContentSource`. The prototype uses `createHttpContentSource` pointed at the
 * bundled sample manifest (public/content/manifest.json). To go live, point
 * VITE_MANIFEST_URL at the manifest the AWS pipeline publishes — or implement
 * another ContentSource (e.g. a REST API) with the same two methods.
 *
 * No credentials are used or needed: the manifest, briefing bodies and MP3s
 * must be publicly readable (typically through CloudFront).
 */
import type { BriefingBlock, BriefingBody, Edition, Manifest, PublicationStatus } from './types';

export interface ContentSource {
  loadManifest(signal?: AbortSignal): Promise<Manifest>;
  loadBriefingBody(editionId: string, signal?: AbortSignal): Promise<BriefingBlock[]>;
  /** Resolve a manifest-relative asset URL (audio, body) to an absolute URL. */
  resolveUrl(url: string): string;
}

export class ContentError extends Error {
  constructor(message: string, readonly cause?: unknown) {
    super(message);
    this.name = 'ContentError';
  }
}

const STATUSES: PublicationStatus[] = ['published', 'draft', 'processing', 'failed', 'withdrawn'];
const isStr = (v: unknown): v is string => typeof v === 'string';
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/** Defensive normalisation so a malformed entry cannot break the page. */
export function normaliseManifest(raw: unknown): Manifest {
  if (!raw || typeof raw !== 'object') throw new ContentError('Manifest is not a JSON object');
  const m = raw as Record<string, any>;
  if (m.schemaVersion !== 1) throw new ContentError(`Unsupported manifest schemaVersion: ${String(m.schemaVersion)}`);
  const pub = m.publication ?? {};
  const editions: Edition[] = [];
  for (const e of Array.isArray(m.editions) ? m.editions : []) {
    if (!e || !isStr(e.editionId)) {
      console.warn('[content] skipped edition without editionId', e);
      continue;
    }
    const b = e.briefing;
    const ep = e.episode;
    editions.push({
      editionId: e.editionId,
      topics: Array.isArray(e.topics) ? e.topics.filter(isStr) : [],
      briefing:
        b && isStr(b.title) && isStr(b.publishedAt)
          ? {
              status: STATUSES.includes(b.status) ? b.status : 'draft',
              title: b.title,
              summary: isStr(b.summary) ? b.summary : '',
              recap: isStr(b.recap) ? b.recap : undefined,
              takeaways: Array.isArray(b.takeaways) ? b.takeaways.filter(isStr) : [],
              publishedAt: b.publishedAt,
              body: Array.isArray(b.body) ? b.body : undefined,
              bodyUrl: isStr(b.bodyUrl) ? b.bodyUrl : undefined,
              sources: Array.isArray(b.sources)
                ? b.sources.filter((s: any) => s && isStr(s.label) && isStr(s.url))
                : [],
              wordCount: isNum(b.wordCount) ? b.wordCount : undefined,
              readingMinutes: isNum(b.readingMinutes) ? b.readingMinutes : undefined,
            }
          : null,
      episode:
        ep && isStr(ep.title)
          ? {
              status: STATUSES.includes(ep.status) ? ep.status : 'draft',
              episodeId: isStr(ep.episodeId) ? ep.episodeId : `ep-${e.editionId}`,
              title: ep.title,
              description: isStr(ep.description) ? ep.description : '',
              publishedAt: isStr(ep.publishedAt) ? ep.publishedAt : null,
              hosts: Array.isArray(ep.hosts) ? ep.hosts.filter(isStr) : [],
              audioUrl: isStr(ep.audioUrl) && ep.audioUrl ? ep.audioUrl : null,
              audioMimeType: isStr(ep.audioMimeType) ? ep.audioMimeType : null,
              audioDurationSeconds: isNum(ep.audioDurationSeconds) ? ep.audioDurationSeconds : null,
              audioBytes: isNum(ep.audioBytes) ? ep.audioBytes : null,
              isDemoAudio: ep.isDemoAudio === true,
            }
          : null,
    });
  }
  return {
    schemaVersion: 1,
    generatedAt: isStr(m.generatedAt) ? m.generatedAt : '',
    isSampleContent: m.isSampleContent === true,
    publication: {
      name: isStr(pub.name) ? pub.name : 'Market Intelligence Podcast',
      description: isStr(pub.description) ? pub.description : '',
      timeZone: isStr(pub.timeZone) ? pub.timeZone : 'America/New_York',
      hosts: Array.isArray(pub.hosts) ? pub.hosts.filter(isStr) : [],
    },
    editions,
  };
}

const BLOCK_TYPES = new Set(['heading', 'paragraph', 'list', 'quote', 'table']);
function normaliseBlocks(raw: unknown): BriefingBlock[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((b) => b && typeof b === 'object' && BLOCK_TYPES.has((b as any).type)) as BriefingBlock[];
}

async function fetchJson(url: string, signal?: AbortSignal): Promise<unknown> {
  let res: Response;
  try {
    res = await fetch(url, { signal, headers: { Accept: 'application/json' }, cache: 'no-cache' });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw new ContentError('Network error while loading content', err);
  }
  if (!res.ok) throw new ContentError(`Content request failed (${res.status})`);
  try {
    return await res.json();
  } catch (err) {
    throw new ContentError('Content is not valid JSON', err);
  }
}

export function createHttpContentSource(manifestUrl: string): ContentSource {
  const absoluteManifest = new URL(manifestUrl, document.baseURI).toString();
  const resolveUrl = (u: string) => new URL(u, absoluteManifest.replace(/[^/]*$/, '')).toString();
  let manifestCache: Promise<Manifest> | null = null;
  const bodyCache = new Map<string, Promise<BriefingBlock[]>>();

  const loadManifest = (_signal?: AbortSignal) => {
    if (!manifestCache) {
      // The request is shared and cached, so it is deliberately not tied to one caller's AbortSignal.
      manifestCache = fetchJson(absoluteManifest).then(normaliseManifest);
      manifestCache.catch(() => (manifestCache = null));
    }
    return manifestCache;
  };

  return {
    loadManifest,
    resolveUrl,
    async loadBriefingBody(editionId) {
      const cached = bodyCache.get(editionId);
      if (cached) return cached;
      const p = (async () => {
        const manifest = await loadManifest();
        const b = manifest.editions.find((e) => e.editionId === editionId)?.briefing;
        if (!b) throw new ContentError(`No briefing for edition ${editionId}`);
        if (b.body) return normaliseBlocks(b.body);
        if (!b.bodyUrl) return [];
        // Manifest-relative body URLs resolve against the manifest location.
        const json = (await fetchJson(resolveUrl(b.bodyUrl))) as Partial<BriefingBody>;
        return normaliseBlocks(json?.blocks);
      })();
      bodyCache.set(editionId, p);
      p.catch(() => bodyCache.delete(editionId));
      return p;
    },
  };
}

/** Default source for this build. Override with VITE_MANIFEST_URL at build time. */
export const MANIFEST_URL: string = import.meta.env.VITE_MANIFEST_URL || 'content/manifest.json';
