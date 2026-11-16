# Market Intelligence Podcast — front end

A static React + TypeScript (Vite) site for the Market Intelligence Podcast: a daily financial briefing and a conversational episode hosted by Arthur Bennett and Jack Carter.

This repository is the **front end only**. It reads a published JSON content manifest and never talks to AWS APIs directly. The generation pipeline (Lambda, Step Functions, EC2 audio worker, private S3) is unchanged and **is not connected** in this version — everything shipped here is labelled sample content.

## Quick start

Requires Node.js 18+ (20 LTS recommended).

```bash
npm install
npm run dev        # http://localhost:5173 with hot reload
npm run typecheck  # TypeScript only
npm run build      # type-check + production build into dist/
npm run preview    # serve dist/ locally on http://localhost:4173
```

`dist/` is a plain static folder (HTML, CSS, JS, `content/`). It can be hosted anywhere that serves files; there is no server runtime and no proprietary hosting dependency. Asset paths are relative (`base: './'`) and routing uses the URL hash (`#/briefings/2026-11-13`), so no rewrite rules are required.

Dependencies are deliberately minimal: `react`, `react-dom` at runtime; `vite`, `@vitejs/plugin-react`, `typescript` and type packages for development. Fonts (Switzer, Source Serif 4, JetBrains Mono) load from Fontshare / Google Fonts in `index.html`; self-host them if you want zero third-party requests.

## Project structure

```
index.html                 Font links + tiny inline script that applies the saved theme before first paint
public/content/            Sample content served as static files
  manifest.json            The content manifest (single entry point)
  briefings/*.json         Briefing bodies (structured blocks)
  audio/*.mp3              Two short demo episodes (synthetic voices)
src/
  content/types.ts         Manifest schema (the contract with the pipeline)
  content/source.ts        ContentSource interface + HTTP implementation, validation/normalisation
  content/selectors.ts     Published-only filtering, "latest" logic, search, reading time
  state/content.tsx        Content provider (+ prototype scenarios: loading / empty / error / episode pending)
  state/player.tsx         One shared <audio> element for the whole app
  state/theme.tsx          Theme state, persisted in localStorage (falls back to memory)
  lib/                     Formatting (dates, time zones, durations), storage, hash router
  components/              Shell (nav, theme selector, footer), BriefingPanel, EpisodePlayer, lists, safe block renderer
  pages/                   Dashboard, Briefings / Episodes archives, Article view, sidebarModules.ts
  styles/                  tokens.css (both themes), base.css, components.css
```

## Content manifest

The manifest URL is set at build time with `VITE_MANIFEST_URL` (see `.env.example`). Default: `content/manifest.json` relative to the site. An absolute URL (e.g. `https://content.example.com/mip/manifest.json`) also works.

```jsonc
{
  "schemaVersion": 1,
  "generatedAt": "2026-11-13T07:31:00-05:00",
  "isSampleContent": true,              // remove/false for real content; drives the "Sample" labels
  "publication": {
    "name": "Market Intelligence Podcast",
    "description": "…",
    "timeZone": "America/New_York",     // IANA zone used to display times (shown with UTC offset)
    "hosts": ["Arthur Bennett", "Jack Carter"]
  },
  "editions": [
    {
      "editionId": "2026-11-13",
      "topics": ["Macro", "Rates", "AI"],
      "briefing": {
        "status": "published",          // published | draft | processing | failed | withdrawn
        "title": "…",
        "summary": "Opening recap paragraph",
        "recap": "Optional 60–90 word recap for the Forest homepage (falls back to summary)",
        "takeaways": ["…", "…"],
        "publishedAt": "2026-11-13T06:45:00-05:00",
        "bodyUrl": "briefings/2026-11-13.json",   // or "body": [ …blocks… ] inline
        "sources": [{ "label": "…", "url": "https://…", "publisher": "…" }]
      },
      "episode": {
        "status": "published",
        "episodeId": "mip-2026-11-13",
        "title": "…",
        "description": "…",
        "publishedAt": "2026-11-13T07:30:00-05:00",
        "hosts": ["Arthur Bennett", "Jack Carter"],
        "audioUrl": "audio/mip-2026-11-13.mp3",   // null when no file exists
        "audioMimeType": "audio/mpeg",
        "audioDurationSeconds": 1520,
        "audioBytes": 24320000
      }
    }
  ]
}
```

Rules the UI applies:

- Only items with `status: "published"` and `publishedAt` in the past are shown. Drafts and processing items in the manifest are ignored (the sample manifest contains one to prove it).
- Briefing and episode are independent: either may be `null` or not yet published. If the newest briefing has no published episode, the sidebar shows the most recent episode under **"Latest available episode"** with its real date and a note that the matching episode is still in production. An older episode is never labelled as today's.
- Relative `bodyUrl` / `audioUrl` values resolve against the manifest's own URL, so the content folder can move as a unit.
- Play and download controls appear only when `audioUrl` is present; otherwise the row says "Audio not available".

### Briefing body format (`mi-blocks/v1`)

```json
{ "editionId": "2026-11-13", "format": "mi-blocks/v1", "blocks": [
  { "type": "heading", "text": "Markets at a glance" },
  { "type": "paragraph", "text": "Supports **bold** and [links](https://example.com)." },
  { "type": "list", "items": ["…"], "ordered": false },
  { "type": "quote", "text": "…", "attribution": "…" },
  { "type": "table", "caption": "…", "columns": ["Asset", "Level", "Week"], "rows": [["…", "…", "+0.8%"]] }
]}
```

No HTML is ever injected: text is rendered as React text nodes, only `**bold**` and `[label](https://…)` are interpreted, and only `http(s)` links are allowed (opened with `rel="noopener noreferrer"`). Table cells starting with `+` / `−` get the positive / negative colours — the only place strong green and red are used.

### Swapping the content source

`src/content/source.ts` exposes a `ContentSource` interface (`getManifest()`, `getBriefingBody()`). The default implementation fetches static JSON. To read from somewhere else (an API Gateway endpoint, a CMS), implement the interface and pass it to `<ContentProvider source={…}>` in `src/App.tsx`. Nothing else in the UI changes.

## Connecting the AWS pipeline later

Recommended shape — keep the existing private bucket private and add a **publish step** at the end of the Step Functions workflow:

1. When a briefing (and later its episode) is approved, the workflow copies the public artefacts — `briefings/<id>.json`, `audio/<episodeId>.mp3` — into a **public-content prefix** (a separate bucket or prefix), then rewrites `manifest.json` with the new edition and `status: "published"`. Write the manifest last so readers never see a reference to a file that does not exist yet.
2. Serve that prefix through **CloudFront with Origin Access Control (OAC)**. The bucket stays non-public; only CloudFront can read it. Browsers receive plain HTTPS URLs.
3. **No credentials in the browser.** The front end performs anonymous `GET`s only. Never place AWS keys, signed-URL secrets or private bucket names in `VITE_*` variables — they are compiled into public JavaScript. If episodes ever need to be private, issue CloudFront signed URLs/cookies from a small backend endpoint.

Integration details to get right:

- **CORS.** Not needed if content is served from the same CloudFront distribution as the site (e.g. `/content/*` behaviour). If content lives on another domain, add a CloudFront response-headers policy (or S3 CORS rule) allowing `GET, HEAD` from the site origin and exposing `Content-Length, Content-Range, Accept-Ranges`.
- **Seeking.** `<audio>` seeks with HTTP Range requests. S3 and CloudFront support them natively; make sure no Lambda@Edge / function strips the `Range` header and that responses keep `Accept-Ranges: bytes` and `206 Partial Content`. Set `Content-Type: audio/mpeg`.
- **Downloads.** The `download` attribute on a link is ignored by browsers for **cross-origin** URLs (the file would open in a player instead). To make "Download MP3" always save the file, either serve audio from the same origin, or set `Content-Disposition: attachment; filename="mip-2026-11-13.mp3"` on the object (S3 metadata at upload time). Note: one object with `attachment` still plays fine in `<audio>`.
- **Caching.** Short TTL (e.g. `Cache-Control: max-age=60`) or an invalidation for `manifest.json`; long-lived `max-age=31536000, immutable` for MP3 and briefing files, using unique filenames per edition.
- **Status.** The pipeline can include drafts/processing items in the manifest for observability; the UI filters them out. Use `failed`/`withdrawn` to pull an item without deleting files.

## Hosting the site on S3 + CloudFront

```bash
npm run build
aws s3 sync dist/ s3://<site-bucket>/ --delete \
  --exclude "index.html" --cache-control "public,max-age=31536000,immutable"
aws s3 cp dist/index.html s3://<site-bucket>/index.html --cache-control "no-cache"
```

- Bucket: Block Public Access **on**; CloudFront distribution with OAC as the only reader; default root object `index.html`; HTTPS only.
- Hashed files in `assets/` can be cached forever; `index.html` should revalidate.
- Because routes live in the hash, no 403/404 → `index.html` error rewrites are needed.
- If the sample `content/` folder is replaced by pipeline output on another path, set `VITE_MANIFEST_URL` before building, or keep a `/content/*` behaviour on the same distribution pointing at the public-content prefix.
- A custom domain is just an ACM certificate (us-east-1) + alternate domain name on the distribution + a DNS alias record — not required for the preview.

## Extending the dashboard

Future modules (indices, bond yields, FX & commodities, gainers/losers, earnings calendar, news) slot in beneath the player through `src/pages/sidebarModules.ts`:

```ts
import { MarketIndicesPanel } from '../modules/MarketIndicesPanel';
export const sidebarModules = [MarketIndicesPanel];
```

Each module receives the manifest and renders a `<Panel>` from `components/ui.tsx`. Add the data it needs to the manifest (e.g. `edition.markets`) or give it its own `ContentSource`. Label non-live data with `<Tag tone="sample">`, and use `.pos` / `.neg` classes (or `+`/`−` table cells) for performance colours. The array is empty in v1 on purpose: no fake prices, no placeholder panels.

## Themes

Two themes, defined entirely as CSS variables in `src/styles/tokens.css`:

| Token | Forest | Dark blue |
|---|---|---|
| Background | `#0B1713` | `#0B1220` |
| Panels | `#12251D` | `#131F33` |
| Text | `#F2F3ED` | `#EDF2F7` |
| Accent | `#9CC5AC` | `#8CB4E8` |

The selection is stored in `localStorage` under `mip.theme` and applied before first paint by an inline script in `index.html`. Switching only changes `data-theme` on `<html>`, so scroll position, expanded state and audio playback are untouched. Measured contrast on panels: body text ≈ 14.4:1, muted text ≈ 9.7:1, faint labels ≈ 6.2:1, accent ≈ 7.7–8.4:1, positive/negative ≈ 6–9.6:1 (all above WCAG AA 4.5:1).

### Forest homepage (light dashboard)

The Forest **dashboard only** uses a light reading layout: forest-green masthead (`#123B2D`), warm off-white page (`#F5F5F0`), white panels, charcoal text (`#202923`) and forest-green buttons (`#1E5C44`). It is scoped so the Blue theme and every other page are unchanged:

- `App.tsx` (and the bootstrap script in `index.html`) sets `data-page="dashboard" | "other"` on `<html>`.
- `src/styles/forest-home.css` scopes every rule to `:root[data-theme='forest'][data-page='dashboard']`.
- `Dashboard`, `BriefingPanel`, `EpisodePlayer` and the list rows take a `refined` / `simple` prop, enabled only for Forest, to render the simplified markup (collapsed recap, “Episode details” disclosure, lighter player).
- Because the two layouts differ in height, `src/state/theme.tsx` keeps the paragraph you are reading in place when switching themes.

Measured contrast on white panels: text 15.0:1, muted 7.2:1, faint 5.4:1, accent 7.9:1, positive 6.2:1, negative 6.6:1, sample note 6.1:1.

## Design decisions

- **Dashboard, not landing page.** A one-line edition header with publication status replaces a hero. Desktop uses a 2/3 briefing column and 1/3 sticky player; tablet and mobile place a compact player above the briefing.
- **Typography does the work.** Switzer for interface, Source Serif 4 for long-form reading (≈ 70-character measure, 1.6 line-height), JetBrains Mono only for dates, times, durations and figures.
- **Inline disclosure for the briefing.** The recap and takeaways are always visible; "Expand full briefing" reveals only the remaining sections (no duplication). It is a real `<button aria-expanded aria-controls>`; collapse controls sit at the top and bottom, and collapsing from the bottom scrolls back to the briefing header and returns focus to the toggle. No inner scroll area, no animation beyond a short fade.
- **One audio element.** A single `<audio preload="none">` lives at the app root, so only one episode plays at a time and navigation, expansion and theme changes never restart it. A slim now-playing bar appears once playback starts. Nothing autoplays.
- **Honest states.** Times show their zone and UTC offset; demo content, demo audio and sample figures are labelled; missing audio is stated rather than faked; loading, empty and error states exist (switchable from the footer for review).

## Known limitations

- All briefings, figures and episodes are sample content. Two episodes have short demo MP3s generated with synthetic voices; three have no audio on purpose to show the unavailable state.
- AWS publishing is not connected; the manifest is a static file in `public/content/`.
- Search is client-side over titles, summaries, topics and dates (fine for hundreds of editions; move to a search index for thousands).
- Volume control is hidden where the browser does not allow scripted volume (iOS Safari); the device buttons are used instead.
- Fonts are loaded from third-party CDNs.
- In sandboxed preview environments `localStorage` can be blocked; the theme then lasts for the session only.
