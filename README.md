# SMH / AFR Bypass Proxy

A single-file Flask reverse proxy that serves cleaned, readable pages from
The Sydney Morning Herald (smh.com.au) and the Australian Financial Review
(afr.com) without ads, trackers, or paywall chrome.

## Running

```sh
docker compose up -d --build
```

- Listens on port **5008** (container `smh-bypass`, external `edge` network).
- SMH is served at `/`, AFR under the `/afr/` prefix.
- `app.py` is baked into the image — after editing, rebuild with
  `docker compose up -d --build`.

## Routes

| Route | Purpose |
|---|---|
| `/` , `/<path>` | SMH pages (homepage, topics, articles) |
| `/afr/` , `/afr/<path>` | AFR pages |
| `/__more?kind=tag&tag=<id>&brand=<smh\|afr>&since=<cursor>` | JSON batch of article cards for "Show more" pagination |
| `/__mostviewed` | JSON list of most-viewed articles |

## Features

- **Clean article rendering** — articles are re-rendered from the embedded
  hydration data: headline, byline (resolved through Apollo `__ref`
  normalization), overview, body, hero image. The publish date is rendered
  as a `<time>` element and converted to the viewer's local timezone by a
  small inline script.
- **Homepage/topic cleanup** — ads, paywall banners, partner blocks, and
  non-article strips (newsletter/podcast/weather) are removed, along with
  the empty wrapper shells they leave behind.
- **AFR-specific cleanup** — removes the "Today's Paper" top bar (desktop
  and mobile variants), the market-snapshot loading bar, the sticky
  leaderboard placeholder, and newsletter driver tiles. Lazy-loaded images
  (`data-src` / `data-srcset`) are promoted to `src` / `srcset` since the
  swap-in scripts are blocked.
- **"Show more" pagination** — topic pages on both brands get a working
  "Show more" button that fetches the next batch of articles from the
  respective FFX GraphQL API (`api.ffx.io` for SMH, `api.afr.com` for AFR)
  and appends them as cards. On AFR the button is placed at the end of the
  main article list, above the AFR Magazine pre-footer section. AFR topic
  pages are server-rendered, so the already-shown article IDs are recovered
  from the `-YYYYMMDD-p<id>` href suffixes. The card grid collapses to a
  single column below 768px to match the native mobile list layout.

## Files

- `app.py` — the entire proxy (Flask app, HTML cleanup, GraphQL clients).
- `Dockerfile` — `python:3.12-slim` + requirements.
- `docker-compose.yml` — service definition, port 5008, `edge` network.
- `requirements.txt` — flask, requests, beautifulsoup4.
