/** Date, time and duration formatting. All times are shown in the publication's time zone. */

const cache = new Map<string, Intl.DateTimeFormat>();
function fmt(locale: string, opts: Intl.DateTimeFormatOptions) {
  const key = locale + JSON.stringify(opts);
  let f = cache.get(key);
  if (!f) cache.set(key, (f = new Intl.DateTimeFormat(locale, opts)));
  return f;
}

/** "Fri 13 Nov 2026" */
export function formatDate(iso: string, timeZone: string): string {
  return fmt('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone }).format(new Date(iso)).replace(',', '');
}

/** "Friday 13 November 2026" */
export function formatLongDate(iso: string, timeZone: string): string {
  return fmt('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone }).format(new Date(iso)).replace(',', '');
}

/** "13 Nov" */
export function formatShortDate(iso: string, timeZone: string): string {
  return fmt('en-GB', { day: 'numeric', month: 'short', timeZone }).format(new Date(iso));
}

/** ISO yyyy-mm-dd in the publication zone (for <time dateTime> and compact lists). */
export function isoDay(iso: string, timeZone: string): string {
  const p = fmt('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone }).format(new Date(iso));
  return p;
}

/** { time: "06:45", zone: "EST", offset: "UTC−05:00" } */
export function formatTimeParts(iso: string, timeZone: string) {
  const d = new Date(iso);
  const time = fmt('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone }).format(d);
  const zone =
    fmt('en-US', { timeZone, timeZoneName: 'short' })
      .formatToParts(d)
      .find((p) => p.type === 'timeZoneName')?.value ?? timeZone;
  let offset = '';
  try {
    const raw =
      fmt('en-US', { timeZone, timeZoneName: 'longOffset' } as Intl.DateTimeFormatOptions)
        .formatToParts(d)
        .find((p) => p.type === 'timeZoneName')?.value ?? '';
    offset = raw === 'GMT' ? 'UTC+00:00' : raw.replace('GMT', 'UTC').replace('-', '−');
  } catch {
    /* older engines */
  }
  return { time, zone, offset };
}

/** Human-friendly zone city, e.g. America/New_York → "New York" */
export const zoneCity = (tz: string) => tz.split('/').pop()!.replace(/_/g, ' ');

/** 126 → "2:06", 3725 → "1:02:05" */
export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || !Number.isFinite(totalSeconds) || totalSeconds < 0) return '–:––';
  const s = Math.round(totalSeconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

/** Spoken form for aria-valuetext: "1 minute 4 seconds" */
export function spokenDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  const parts = [];
  if (m) parts.push(`${m} minute${m === 1 ? '' : 's'}`);
  parts.push(`${sec} second${sec === 1 ? '' : 's'}`);
  return parts.join(' ');
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return '';
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export const formatNumber = (n: number) => new Intl.NumberFormat('en-GB').format(n);
