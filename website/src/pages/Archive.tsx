import { useId, useMemo, useState, type ReactNode } from 'react';
import { publishedBriefings, publishedEpisodes, searchItems, type SortOrder } from '../content/selectors';
import { useContent } from '../state/content';
import { BriefingRow, EpisodeRow } from '../components/Lists';
import { SearchIcon } from '../components/Icons';
import { SkeletonLines, StateMessage } from '../components/ui';

function ArchiveControls({
  query,
  setQuery,
  order,
  setOrder,
  count,
  total,
  noun,
}: {
  query: string;
  setQuery: (q: string) => void;
  order: SortOrder;
  setOrder: (o: SortOrder) => void;
  count: number;
  total: number;
  noun: string;
}) {
  const id = useId();
  return (
    <div className="archive-controls" role="search" aria-label={`Search ${noun}`}>
      <div className="search-field">
        <label htmlFor={`${id}-q`} className="sr-only">
          Search {noun}
        </label>
        <SearchIcon size={16} className="search-icon" />
        <input
          id={`${id}-q`}
          type="search"
          className="input"
          placeholder={`Search ${noun} by title, topic or date`}
          value={query}
          onChange={(e) => setQuery(e.currentTarget.value)}
          autoComplete="off"
          data-testid="input-archive-search"
        />
      </div>
      <label className="sort-field">
        <span className="faint">Order</span>
        <select className="select" value={order} onChange={(e) => setOrder(e.currentTarget.value as SortOrder)} data-testid="select-archive-order">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
      </label>
      <p className="result-count mono" aria-live="polite" data-testid="text-result-count">
        {count === total ? `${total} ${noun}` : `${count} of ${total} ${noun}`}
      </p>
    </div>
  );
}

function ArchiveShell({ title, intro, children }: { title: string; intro: string; children: ReactNode }) {
  return (
    <div className="container page-pad archive">
      <header className="page-head">
        <h1 className="page-title">{title}</h1>
        <p className="faint page-intro">{intro}</p>
      </header>
      {children}
    </div>
  );
}

// Remembered for the session so returning from an article keeps the search.
const memo: Record<string, { query: string; order: SortOrder }> = {};
function useArchiveState(key: string) {
  const [query, setQ] = useState(memo[key]?.query ?? '');
  const [order, setO] = useState<SortOrder>(memo[key]?.order ?? 'newest');
  const setQuery = (q: string) => {
    memo[key] = { query: q, order };
    setQ(q);
  };
  const setOrder = (o: SortOrder) => {
    memo[key] = { query, order: o };
    setO(o);
  };
  return { query, setQuery, order, setOrder };
}

function LoadingOrError({ noun }: { noun: string }) {
  const { state, retry } = useContent();
  if (state.status === 'loading')
    return (
      <div className="panel" aria-busy="true">
        <SkeletonLines lines={6} />
        <p className="sr-only" role="status">
          Loading {noun}…
        </p>
      </div>
    );
  if (state.status === 'error')
    return (
      <StateMessage
        tone="error"
        title={`The ${noun} archive could not be loaded.`}
        action={
          <button className="btn btn-primary" type="button" onClick={retry}>
            Try again
          </button>
        }
      >
        <p>{state.message}</p>
      </StateMessage>
    );
  return null;
}

export function BriefingsPage() {
  const { state } = useContent();
  const s = useArchiveState('briefings');
  const all = useMemo(() => (state.status === 'ready' ? publishedBriefings(state.manifest) : []), [state]);
  const results = useMemo(
    () => searchItems(all, s.query, s.order, (b) => `${b.summary} ${b.takeaways.join(' ')}`),
    [all, s.query, s.order],
  );
  return (
    <ArchiveShell title="Briefings" intro="Every published daily briefing. Open any edition to read it in full.">
      {state.status !== 'ready' ? (
        <LoadingOrError noun="briefings" />
      ) : all.length === 0 ? (
        <StateMessage title="No briefings have been published yet." />
      ) : (
        <>
          <ArchiveControls {...s} count={results.length} total={all.length} noun="briefings" />
          <div className="panel panel-flush">
            {results.length ? (
              <ul className="rows">
                {results.map((b) => (
                  <BriefingRow key={b.editionId} briefing={b} tz={state.manifest.publication.timeZone} />
                ))}
              </ul>
            ) : (
              <StateMessage
                title={`No briefings match “${s.query}”.`}
                action={
                  <button className="btn btn-secondary" type="button" onClick={() => s.setQuery('')} data-testid="button-clear-search">
                    Clear search
                  </button>
                }
              />
            )}
          </div>
        </>
      )}
    </ArchiveShell>
  );
}

export function EpisodesPage() {
  const { state } = useContent();
  const s = useArchiveState('episodes');
  const all = useMemo(() => (state.status === 'ready' ? publishedEpisodes(state.manifest) : []), [state]);
  const results = useMemo(() => searchItems(all, s.query, s.order, (e) => `${e.description} ${e.hosts.join(' ')}`), [all, s.query, s.order]);
  return (
    <ArchiveShell
      title="Episodes"
      intro="Every published episode with Arthur Bennett and Jack Carter. Play in the browser or download the MP3."
    >
      {state.status !== 'ready' ? (
        <LoadingOrError noun="episodes" />
      ) : all.length === 0 ? (
        <StateMessage title="No episodes have been published yet." />
      ) : (
        <>
          <ArchiveControls {...s} count={results.length} total={all.length} noun="episodes" />
          <div className="panel panel-flush">
            {results.length ? (
              <ul className="rows">
                {results.map((e) => (
                  <EpisodeRow key={e.episodeId} episode={e} tz={state.manifest.publication.timeZone} />
                ))}
              </ul>
            ) : (
              <StateMessage
                title={`No episodes match “${s.query}”.`}
                action={
                  <button className="btn btn-secondary" type="button" onClick={() => s.setQuery('')} data-testid="button-clear-search">
                    Clear search
                  </button>
                }
              />
            )}
          </div>
          <p className="fine-print archive-foot">
            Only the two most recent sample episodes include demo audio. Older sample entries show an honest “Audio not available” state.
          </p>
        </>
      )}
    </ArchiveShell>
  );
}
