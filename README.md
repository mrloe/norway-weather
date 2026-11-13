# Norway weather watch

Forecast and official warnings for 48 Norwegian cities, 10 days ahead, from MET Norway (the data behind Yr).
Standard library only, Python 3.9+. Edit `cities.json` to change the city list.

## Run it (Windows, from this folder)

1. Open `config.json`, set `user_agent` to your app name plus your email, e.g.
   `"NorwayWeatherWatch/1.0 you@mail.com"`. MET blocks anonymous clients (HTTP 403).
2. `py engine.py` (or `python engine.py`) builds the `site` folder.
3. Open `site/index.html`, or run `py engine.py --serve` and open http://localhost:8000.

`py engine.py --demo` builds the page from synthetic data, with a loud banner, to preview the layout offline.
`py engine.py --watch 30 --serve` rebuilds every 30 minutes and serves it on your machine.
Tests: `py -m unittest discover -s tests`.

## What you get in `site/`

| File | Use |
|---|---|
| `index.html` | the dashboard: city x day matrix, click a cell for details and a chart. Print it to PDF for a snapshot. |
| `daily.csv` | one row per city per day, tidy. Feed this to Looker Studio, Gemini, BigQuery or Excel. |
| `alerts.csv` | active official warnings and the cities they cover |
| `digest.txt` | plain-text morning summary, paste into Slack or email |
| `data.json` | everything the page shows |
| `status.json` | machine-readable data status (when fetched, which sources worked, fresh/aging/stale at write time), for a watchdog |

## How to read it

- Yellow, orange and red cells are official MET warnings (land only; sea warnings are off by default).
- Purple cells are our own flag from the forecast numbers. They are not official warnings. The limits are in
  `config.json` and shown on the page, and each flagged day lists exactly which number crossed which limit.
- Official warnings only exist once MET has issued them, which is usually only for the nearest days.
  Later days therefore rely on the purple forecast flags.
- Wind is mean wind (the compact forecast has no gusts). After about 60 hours the forecast is in 6-hour steps.

## How much to trust the data

The page shows a status bar at the top: **Fresh** (green), **Aging** (amber) or **Stale** (red), with the time the data
was fetched and its age. The age keeps counting up in the browser, so if the engine stops, the page turns amber after
90 minutes and red (dimmed, "do not rely") after 3 hours. Click Details for per-source status: how many city forecasts
succeeded, which warning feeds worked, and when each was last confirmed by MET. If MET cannot be reached, the engine
reuses its last good data and says so. If no warning feed works, the page says warning data is unavailable instead of
showing zero warnings. Limits are `fresh_minutes` and `stale_after_minutes` in `config.json`.

## Share it with colleagues (free, automatic)

Put this folder in a GitHub repo and use `.github/workflows/update.yml`. It rebuilds every 30 minutes and
publishes to GitHub Pages. Setup steps are in the comments at the top of that file.
Anything that can run Python on a schedule and host static files works too (the output is plain files).

## Data handling

- Follows MET's terms: identifying User-Agent, gzip, honours `Expires` and `If-Modified-Since` (cache in `.cache/`).
- Warnings come from the RSS feed (with polygons), enriched by the GeoJSON feed. Either alone is enough as a fallback.
- If warning data cannot be fetched, the page says so: no warnings shown then does NOT mean none exist.
- Data: MET Norway, CC BY 4.0.
