import type { ReactNode } from 'react';
import { formatDate, formatTimeParts, isoDay } from '../lib/format';

/** Generic dashboard panel. Future market widgets (indices, yields, earnings) should use this. */
export function Panel({
  as: As = 'section',
  label,
  labelId,
  actions,
  children,
  className = '',
  ...rest
}: {
  as?: 'section' | 'article' | 'div' | 'aside';
  label?: ReactNode;
  labelId?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  [k: string]: unknown;
}) {
  return (
    <As className={`panel ${className}`} aria-labelledby={label && labelId ? labelId : undefined} {...rest}>
      {(label || actions) && (
        <div className="panel-head">
          {label && (
            <h2 className="eyebrow" id={labelId}>
              {label}
            </h2>
          )}
          {actions && <div className="panel-actions">{actions}</div>}
        </div>
      )}
      {children}
    </As>
  );
}

export function Tag({ children, tone = 'default' }: { children: ReactNode; tone?: 'default' | 'sample' | 'warn' | 'muted' }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

export function TopicTags({ topics, max = 4 }: { topics: string[]; max?: number }) {
  if (!topics.length) return null;
  const shown = topics.slice(0, max);
  return (
    <ul className="topic-tags" aria-label="Topics">
      {shown.map((t) => (
        <li key={t}>
          <Tag>{t}</Tag>
        </li>
      ))}
      {topics.length > max && (
        <li>
          <Tag tone="muted">+{topics.length - max}</Tag>
        </li>
      )}
    </ul>
  );
}

/** Date with optional time and explicit zone, e.g. "Fri 13 Nov 2026 · 06:45 EST (UTC−05:00)". */
export function PubTime({ iso, tz, withTime = false, compact = false }: { iso: string; tz: string; withTime?: boolean; compact?: boolean }) {
  const date = compact ? isoDay(iso, tz) : formatDate(iso, tz);
  if (!withTime) {
    return (
      <time className="mono" dateTime={iso}>
        {date}
      </time>
    );
  }
  const { time, zone, offset } = formatTimeParts(iso, tz);
  return (
    <time className="mono" dateTime={iso} title={`${tz.replace(/_/g, ' ')} time`}>
      {date} · {time} {zone}
      {offset && <span className="faint"> ({offset})</span>}
    </time>
  );
}

export function SampleNotice({ children }: { children: ReactNode }) {
  return (
    <p className="sample-notice" role="note">
      <span className="sample-notice-tag">Demo</span>
      <span>{children}</span>
    </p>
  );
}

export function SkeletonLines({ lines = 3, className = '' }: { lines?: number; className?: string }) {
  return (
    <div className={`skeleton-group ${className}`} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton" style={{ width: `${i === lines - 1 ? 62 : 100 - i * 6}%` }} />
      ))}
    </div>
  );
}

export function StateMessage({
  title,
  children,
  action,
  tone = 'neutral',
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  tone?: 'neutral' | 'error';
}) {
  return (
    <div className={`state-msg state-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <p className="state-title">{title}</p>
      {children && <div className="state-body">{children}</div>}
      {action && <div className="state-action">{action}</div>}
    </div>
  );
}
