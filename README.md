# Ohio lake conditions — Cracked Buckeye's hourly lake mirror

Part of **Cracked Buckeye** (the Ohio knowledge database; the atlas repo `ohio-tap-water-atlas` is its home and
indexes this one). Owner's ask, 2026-10-02: *"create a permanent code to update the lake levels, temperatures, and
thermocline for each ohio lake by mirroring the USGS site data… set reading for every hour… do wind conditions and any
other data they have too."*

Every hour, a GitHub Actions job (`.github/workflows/hourly.yml`, at :17) reads every live series at 34 Ohio lakes and:

1. **archives** each reading the first time it is seen, in `history/YYYY/MM/DD/HH.csv.gz` (write-once, gzip CSV:
   `time_utc, lake, source, series, value, unit, flag`) — the permanent record, committed to this repo;
2. **publishes** the current picture to the Cracked Buckeye R2 bucket, `lakes-live/`, which crackedbuckeye.com's lake
   pages read: `now.json` (every lake's headline numbers) and `lake/<slug>.json` (one lake: every series, latest
   value, 24-hour and 7-day change, an hourly 7-day trace, the Corps' forecast and guide curve, the weather station).

## Sources (public, no key)

| Source | What | API |
|---|---|---|
| U.S. Geological Survey | lake level, water temperature, stream flow and stage above and below each lake, dissolved oxygen, pH, specific conductance, rainfall, groundwater — every continuous series at the gauges each lake deep dive chose | `api.waterdata.usgs.gov/ogcapi/v0/collections/continuous` (the WaterServices API it replaces is retired in early 2027) |
| U.S. Army Corps of Engineers (CWMS) | pool level, storage, inflow, release, tailwater, released-water temperature, rainfall at the dam, the Corps' 7–10-day level and flow forecast, the seasonal guide curve | `cwms-data.usace.army.mil/cwms-data/timeseries` (offices LRH Huntington, LRL Louisville, LRP Pittsburgh) |
| National Weather Service (METAR via aviationweather.gov) | wind speed, gusts and direction, air temperature, dew point, pressure, visibility at the nearest airport station | `aviationweather.gov/api/data/metar` |

All of it is provisional and may be revised by the agencies; the archive keeps readings **as first published**.
The agencies hold the reviewed record.

**Thermocline:** the only live temperature-by-depth readings on these lakes are the Army Corps' seasonal buoys at
Berlin (24 depths) and Kirwan (19), about June to October, mirrored hourly with a thermocline estimate (steepest
drop of at least 1 degC per metre). Out of the water they send -99999, which is filtered. Every lake's historical
summer profile (Water Quality Portal, EPA, Corps records) is built in the atlas repo, `data/lakes/thermocline/`.

## Files

| File | |
|---|---|
| `registry.json` | the 34 lakes and every series read for each (built by `build_registry.py`) |
| `build_registry.py` | rebuild the registry: USGS gauges named in each deep dive's `data/<slug>/live/fetch_live.py` (atlas repo) expanded to every live series at them; the Corps' catalog for each dam; the nearest METAR station. Run by hand when a gauge is added. |
| `fetch.py` | the hourly job. `--hours N` looks back N hours (default 6; it reaches back further by itself after skipped runs, up to a week). `--upload s3` (CI, R2 secrets) or `--upload rclone` (JoelHome's `r2` remote). |
| `state/last_seen.json` | newest archived time per series, plus `_last_run` |
| `public/` | the files uploaded to R2 (not committed) |

## Secrets (repository settings → Secrets → Actions)

`R2_ENDPOINT` (`https://<account>.r2.cloudflarestorage.com`), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` — an R2 token
with Object Read & Write on the `crackedbuckeye` bucket. Optional: `USGS_API_KEY` (free, api.waterdata.usgs.gov/signup)
raises USGS's hourly request limit, which anonymous requests from a shared CI address can hit.

## Coverage (2026-10-02)

Lake level for 29 of 34 lakes (Acton, Cowan, Rocky Fork, Salt Fork and Pymatuning have no public live pool gauge);
in-lake water temperature at Hoover (2 ft below the surface) and Indian Lake; released-water temperature at the Corps
dams that publish it; wind and weather for all 34.

## Rules

- Public-domain government data; credit USGS, USACE and NWS where it is shown.
- Never `rclone sync` to the bucket root: the site's map packs live there. Uploads go to `lakes-live/` only.
