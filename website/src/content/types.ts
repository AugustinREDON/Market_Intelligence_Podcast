/**
 * Content model for Market Intelligence Podcast.
 *
 * These types describe the *published content manifest* — the only contract
 * between the front end and the (separate) AWS generation pipeline. The
 * pipeline should write a JSON document matching `Manifest` to a public,
 * read-only location (e.g. a CloudFront-fronted S3 prefix). See README.md.
 */

export type PublicationStatus = 'published' | 'draft' | 'processing' | 'failed' | 'withdrawn';

export interface SourceLink {
  label: string;
  url: string;
  publisher?: string;
}

/** Structured, safe briefing body. No raw HTML is ever rendered. */
export type BriefingBlock =
  | { type: 'heading'; text: string }
  | { type: 'paragraph'; text: string }
  | { type: 'list'; items: string[]; ordered?: boolean }
  | { type: 'quote'; text: string; attribution?: string }
  | { type: 'table'; caption?: string; columns: string[]; rows: string[][] };

export interface BriefingBody {
  editionId: string;
  format: 'mi-blocks/v1';
  blocks: BriefingBlock[];
}

export interface Briefing {
  status: PublicationStatus;
  title: string;
  /** Opening recap paragraph, always visible above the fold. */
  summary: string;
  /**
   * Optional dashboard recap (~60–90 words) used by the Forest homepage's
   * collapsed briefing. Falls back to `summary` when absent.
   */
  recap?: string;
  /** Main takeaways, shown with the recap (not repeated in the body). */
  takeaways: string[];
  /** ISO 8601 timestamp with offset, e.g. 2026-11-13T06:45:00-05:00 */
  publishedAt: string;
  /** Either inline blocks or a URL (relative to the manifest) to a BriefingBody JSON file. */
  body?: BriefingBlock[];
  bodyUrl?: string;
  sources: SourceLink[];
  /** Optional overrides; otherwise computed from the text. */
  wordCount?: number;
  readingMinutes?: number;
}

export interface Episode {
  status: PublicationStatus;
  episodeId: string;
  title: string;
  description: string;
  publishedAt: string | null;
  hosts: string[];
  /** Public HTTPS (or relative) URL of the MP3. null when no file exists. */
  audioUrl: string | null;
  audioMimeType: string | null;
  audioDurationSeconds: number | null;
  audioBytes: number | null;
  /** True for demonstration audio used in this prototype. */
  isDemoAudio?: boolean;
}

export interface Edition {
  editionId: string;
  topics: string[];
  briefing: Briefing | null;
  episode: Episode | null;
}

export interface Manifest {
  schemaVersion: 1;
  generatedAt: string;
  /** When true the UI labels everything as demonstration content. */
  isSampleContent?: boolean;
  publication: {
    name: string;
    description: string;
    /** IANA zone used to display publication times, e.g. America/New_York */
    timeZone: string;
    hosts: string[];
  };
  editions: Edition[];
}

/* ---------- Derived, UI-facing records (only published content) ---------- */

export interface BriefingItem extends Briefing {
  editionId: string;
  topics: string[];
  isSample: boolean;
}

export interface EpisodeItem extends Episode {
  editionId: string;
  topics: string[];
  publishedAt: string;
  isSample: boolean;
  /** True when a playable/downloadable file exists. */
  hasAudio: boolean;
}
