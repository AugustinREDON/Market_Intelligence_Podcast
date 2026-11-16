import { Fragment, type ReactNode } from 'react';
import type { BriefingBlock, SourceLink } from '../content/types';
import { ExternalIcon } from './Icons';

/**
 * Safe briefing renderer. Content arrives as structured blocks; strings are
 * rendered as React text nodes (auto-escaped). The only inline syntax is
 * **bold** and [label](url), and links are restricted to http(s) or relative
 * URLs. No dangerouslySetInnerHTML anywhere.
 */

export function safeHref(url: string): string | null {
  try {
    const u = new URL(url, document.baseURI);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.toString() : null;
  } catch {
    return null;
  }
}

const isExternal = (href: string) => {
  try {
    return new URL(href).origin !== window.location.origin;
  } catch {
    return true;
  }
};

export function ExtLink({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  const safe = safeHref(href);
  if (!safe) return <>{children}</>;
  const ext = isExternal(safe);
  return (
    <a href={safe} className={className} {...(ext ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>
      {children}
      {ext && (
        <>
          <ExternalIcon className="ext-icon" />
          <span className="sr-only"> (opens in a new tab)</span>
        </>
      )}
    </a>
  );
}

const INLINE = /(\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))/g;

export function Inline({ text }: { text: string }) {
  const parts = text.split(INLINE);
  return (
    <>
      {parts.map((p, i) => {
        if (!p) return null;
        if (p.startsWith('**') && p.endsWith('**')) return <strong key={i}>{p.slice(2, -2)}</strong>;
        const m = p.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
        if (m)
          return (
            <ExtLink key={i} href={m[2]}>
              {m[1]}
            </ExtLink>
          );
        return <Fragment key={i}>{p}</Fragment>;
      })}
    </>
  );
}

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');

/** Signed figures get colour + an arrow glyph, so meaning never relies on colour alone. */
function Cell({ value }: { value: string }) {
  const t = value.trim();
  if (/^[+]/.test(t))
    return (
      <span className="delta pos">
        <span aria-hidden="true">▲ </span>
        {t}
      </span>
    );
  if (/^[−-]/.test(t))
    return (
      <span className="delta neg">
        <span aria-hidden="true">▼ </span>
        {t}
      </span>
    );
  return <>{t}</>;
}

export function BriefingBlocks({ blocks, idPrefix }: { blocks: BriefingBlock[]; idPrefix: string }) {
  return (
    <>
      {blocks.map((b, i) => {
        switch (b.type) {
          case 'heading':
            return (
              <h3 key={i} id={`${idPrefix}-${slug(b.text)}`} className="prose-h">
                {b.text}
              </h3>
            );
          case 'paragraph':
            return (
              <p key={i}>
                <Inline text={b.text} />
              </p>
            );
          case 'list': {
            const L = b.ordered ? 'ol' : 'ul';
            return (
              <L key={i}>
                {b.items.map((it, j) => (
                  <li key={j}>
                    <Inline text={it} />
                  </li>
                ))}
              </L>
            );
          }
          case 'quote':
            return (
              <blockquote key={i}>
                <p>
                  <Inline text={b.text} />
                </p>
                {b.attribution && <footer>— {b.attribution}</footer>}
              </blockquote>
            );
          case 'table':
            return (
              <figure key={i} className="data-table-wrap">
                <div className="data-table-scroll" tabIndex={0} role="region" aria-label={b.caption || 'Data table'}>
                  <table className="data-table">
                    <thead>
                      <tr>
                        {b.columns.map((c, j) => (
                          <th key={j} scope="col">
                            {c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {b.rows.map((r, j) => (
                        <tr key={j}>
                          {r.map((c, k) =>
                            k === 0 ? (
                              <th key={k} scope="row">
                                {c}
                              </th>
                            ) : (
                              <td key={k}>
                                <Cell value={c} />
                              </td>
                            ),
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {b.caption && <figcaption>{b.caption}</figcaption>}
              </figure>
            );
          default:
            return null;
        }
      })}
    </>
  );
}

export function SourceList({ sources, isSample }: { sources: SourceLink[]; isSample: boolean }) {
  if (!sources.length) return null;
  return (
    <section className="sources" aria-label="Sources">
      <h3 className="prose-h sources-h">Sources</h3>
      {isSample && (
        <p className="sources-note">
          General reference portals for the topics covered. The sample briefing's figures are illustrative and are not taken from these pages.
        </p>
      )}
      <ol className="source-list">
        {sources.map((s) => (
          <li key={s.url}>
            <ExtLink href={s.url}>{s.label}</ExtLink>
            {s.publisher && <span className="source-pub"> — {s.publisher}</span>}
          </li>
        ))}
      </ol>
    </section>
  );
}
