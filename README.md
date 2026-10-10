# Ohio lake conditions — Cracked Buckeye's hourly lake mirror

Part of **Cracked Buckeye** (the Ohio knowledge database; the atlas repo `ohio-tap-water-atlas` is its home and
indexes this one). Owner's ask, 2026-10-02: *"create a permanent code to update the lake levels, temperatures, and
thermocline for each ohio lake by mirroring the USGS site data… set reading for every hour… do wind conditions and any
other data they have too."*

Every hour, a GitHub Actions job (`.github/workflows/hourly.yml`, at :17) reads every live series at 34 Ohio lakes, plus
Lake Erie's Ohio shore (see below), and:

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

**Lake Erie (Ohio shore), added 2026-10-10** -- owner: *"can we include lake erie conditions?"* Slug `lakeerie`, public
NOAA sources, no key (`erie.py`):

| Source | What | API |
|---|---|---|
| NOAA CO-OPS | water level (IGLD 1985), water temperature, wind, air temperature, pressure, humidity at the four Ohio gauges: Toledo 9063085, Marblehead 9063079, Cleveland 9063063, Fairport 9063053 (Fairport has no wind or humidity, Cleveland no humidity; the registry lists what each carries) | `api.tidesandcurrents.noaa.gov/api/prod/datagetter` |
| NOAA National Data Buoy Center | 21 buoys and shore stations in Ohio waters, run by NDBC, LimnoTech, the Cleveland Water Alliance and NWS Cleveland and relayed by NDBC: wind, gusts, waves, water and air temperature, pressure | `ndbc.noaa.gov/data/5day2/<ID>_5day.txt` (`realtime2/` for a look-back past 114 hours) |
| National Weather Service Cleveland | Nearshore Marine Forecast for LEZ142 to LEZ149 (Maumee Bay to Ripley NY), the text product, parsed into periods | `tgftp.nws.noaa.gov/data/forecasts/marine/near_shore/le/lez142.txt`, `lez144`, `lez147` |

`api.weather.gov` is not used: its robots.txt disallows every path. The archive stores the agencies' own units (metres,
degC, m/s, hPa); `lake/lakeerie.json` is in feet, knots and degC. Where Lake Erie differs from the inland lakes:

- `now.json` carries it under `great_lakes.lakeerie`, **not** `lakes`, so a site that predates it shows the same 34 rows.
- `lake/lakeerie.json` has `stations` (one record per gauge or buoy, west to east, with `reporting` and `last_report`),
  `groups` (every series, as for the inland lakes, but the 7-day trace is `trace: {t0, v: [...]}`, one value per hour),
  `marine` (the parsed NWS forecast) and a `headline` (Cleveland's water level and temperature, wind from the West Erie
  buoy, the range of wave heights across the buoys that report waves).
- **Buoys come out of the water in the cold months.** A station that has not reported in 3 hours is `reporting: false`;
  its readings are marked stale after 6 hours; `last_report` comes from `state/last_seen.json`, so it stays right for
  weeks. CO-OPS readings that carry one of CO-OPS' own limit or rate-of-change flags are archived (flag `suspect`) but
  never shown. A forecast that cannot be fetched is shown from `state/marine.json`, marked `stale`.
- Each station has its own look-back (`_ok_coops:<id>`, `_ok_ndbc:<id>` in `state/last_seen.json`), and a station seen for
  the first time backfills a week.
- Left out on purpose (see `build_registry.py`): the four NOS stations NDBC repeats (read directly from CO-OPS), the Old
  Woman Creek estuary stations, Toledo Light No. 2 (silent since 15 September).

All of it is provisional and may be revised by the agencies; the archive keeps readings **as first published**.
The agencies hold the reviewed record.

**Thermocline:** the only live temperature-by-depth readings on these lakes are the Army Corps' seasonal buoys at
Berlin (24 depths) and Kirwan (19), about June to October, mirrored hourly with a thermocline estimate (steepest
drop of at least 1 degC per metre). Out of the water they send -99999, which is filtered. Every lake's historical
summer profile (Water Quality Portal, EPA, Corps records) is built in the atlas repo, `data/lakes/thermocline/`.

## Files

| File | |
|---|---|
| `registry.json` | the 34 lakes and every series read for each, and the `lakeerie` entry (built by `build_registry.py`) |
| `build_registry.py` | rebuild the registry: USGS gauges named in each deep dive's `data/<slug>/live/fetch_live.py` (atlas repo) expanded to every live series at them; the Corps' catalog for each dam; the nearest METAR station. Run by hand when a gauge is added. `--erie-only` refreshes just Lake Erie's stations (verifies which CO-OPS products and NDBC columns are live). |
| `erie.py` | Lake Erie: the CO-OPS, NDBC and NWS fetchers and parsers, and the builder of `lake/lakeerie.json` |
| `fetch.py` | the hourly job. `--hours N` looks back N hours (default 6; it reaches back further by itself after skipped runs, up to a week). `--upload s3` (CI, R2 secrets) or `--upload rclone` (JoelHome's `r2` remote). |
| `state/last_seen.json` | newest archived time per series, plus `_last_run` |
| `state/marine.json` | the last NWS marine forecast fetched, shown (marked stale) if a fetch fails |
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
