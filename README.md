# OMV viewer
Recent omgevingsvergunningen (Mercator API layers *stedenbouwkundige projecten* and *verkavelingen*, field `laag`)
for Tervuren, Hoeilaart, Bertem, Overijse and Huldenberg,
plus the running *openbaar onderzoek* periods scraped from the commune websites (`openbaar_onderzoek.py`; Bertem has no source yet).

## Incremental updates
`fetch_data.py` only fetches files changed since the previous run minus 7 days and merges them into `data.json`.
Once a week (or with `python fetch_data.py --full`) it re-fetches everything from 2025, which also drops files
that disappeared from the API.

## When a commune website changes
The commune pages are listed in `sources.json` (one URL or a list of URLs per commune; `type` is `remmicom`, `lcp` or `none`).
- Each run checks every source. Problems show up as a warning box on the site, in the log, and as a failed
  GitHub Actions run (GitHub e-mails you). The data from the sources that still work is saved anyway.
- If an LCP page (Tervuren, Overijse) has moved, the script finds the new page via the sitemap/homepage, keeps working,
  and the warning tells you which URL to put in `sources.json`. Edit the file on github.com, commit, and re-run the workflow.
- No inquiries found for 30 days also counts as a problem (`oo_state.json` remembers the last date).

Note: Tervuren and Overijse only publish the OMV number in the PDF affiche under `/file/`, which their robots.txt
disallows. This is a deliberate choice: each PDF is downloaded once (cached in `oo_cache.json`), with the 1 s crawl delay
they ask for, so it stays at a few files per week. Keep it that way: don't remove the cache or the delay.

## Local test
```
pip install -r requirements.txt
.\run_local.ps1            # full refresh, then opens http://localhost:8000
.\run_local.ps1 -OOOnly    # only re-scrape the commune websites (fast)
.\run_local.ps1 -NoFetch   # just serve the current data.json
```
(Without PowerShell: `python fetch_data.py && python -m http.server`.)

## GitHub
1. Create a public GitHub repo and push this folder.
2. Settings > Pages > Deploy from branch `main`, folder `/ (root)`.
3. Actions > "Update data" > Run workflow. It then runs daily (`.github/workflows/update.yml`).
