"""fetch.py -- the hourly job: current conditions at 34 Ohio lakes, mirrored from USGS, USACE and NOAA/NWS.

    python fetch.py                    # last 6 hours (each run overlaps the last, so a missed hour heals itself)
    python fetch.py --hours 168        # backfill a week
    python fetch.py --upload rclone    # also push public/ to R2 from a machine with the rclone 'r2' remote
    python fetch.py --upload s3        # ... or with R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT set (CI)

Writes
  history/YYYY/MM/DD/HH.csv.gz   every reading first seen this run (write-once; the permanent archive)
  state/last_seen.json           newest time stored per series, so each reading is archived once
  public/now.json                every lake's headline numbers (the /lakes/ conditions table)
  public/lake/<slug>.json        one lake: every series, latest value, 24 h and 7 d change, hourly 7-day trace,
                                 the Corps' forecast and rule curve where it publishes them, the weather station
Sources (all public, no key)
  USGS Water Data API  https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous  (provisional data)
  USACE CWMS Data API  https://cwms-data.usace.army.mil/cwms-data/timeseries          (provisional data)
  NWS METAR via AWC    https://aviationweather.gov/api/data/metar                     (wind, air, pressure)
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
STATE = os.path.join(HERE, "state", "last_seen.json")
PUB = os.path.join(HERE, "public")
UA = {"User-Agent": "CrackedBuckeye/1.0 (crackedbuckeye.com; Ohio lake conditions)"}
USGS_API = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
CWMS_API = "https://cwms-data.usace.army.mil/cwms-data"
AWC_API = "https://aviationweather.gov/api/data"
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
FIELDS = ["time_utc", "lake", "source", "series", "value", "unit", "flag"]

# METAR fields kept, as (key, label, unit)
WX_FIELDS = [("wspd", "Wind speed", "kt"), ("wgst", "Wind gust", "kt"), ("wdir", "Wind direction", "deg"),
             ("temp", "Air temperature", "degC"), ("dewp", "Dew point", "degC"), ("altim", "Pressure", "hPa"),
             ("visib", "Visibility", "mi")]
WX_LABEL = {k: (l, u) for k, l, u in WX_FIELDS}

CWMS_PARAM = {"Elev": "elevation", "Stor": "Storage", "Flow": "flow", "Flow-Inflow": "Inflow",
              "Flow-Inflow-Comp": "Inflow", "Flow-Outflow": "Release (outflow)", "Precip": "Rainfall",
              "Precip-Cum": "Rainfall (running total)", "Precip-Inc": "Rainfall", "Stage": "Stage below the dam",
              "Stage-Tailwater": "Stage below the dam", "Temp-Water": "Water temperature",
              "Temp-Air": "Air temperature"}


# ---------------------------------------------------------------- fetching

def get_json(url, headers=None, tries=4):
    h = dict(UA)
    h.update(headers or {})
    if "api.waterdata.usgs.gov" in url and os.environ.get("USGS_API_KEY"):
        h["X-Api-Key"] = os.environ["USGS_API_KEY"]   # optional free key: a higher hourly limit than anonymous
    ctx = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90, context=ctx) as r:
                return json.load(r)
        except ssl.SSLError:
            # The Corps' Huntington server has served an incomplete certificate chain to some clients; this is
            # read-only public data, so retry once without verification rather than lose the hour.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) or i == tries - 1:
                raise
            # 429: USGS's anonymous hourly limit is shared by everyone on a CI runner's address. A short wait is
            # worth taking; a long one is not -- the source is skipped this hour and the next run reaches back.
            wait = int(e.headers.get("Retry-After") or 0) if e.code == 429 else 0
            if wait > 120:
                raise
            time.sleep(max(wait, 15 * (i + 1)))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def fetch_usgs(reg, hours):
    """{(site, param, time_series_id): [(t, value, unit, approval)]} for every gauge in the registry."""
    sites = sorted({g["site"] for L in reg["lakes"] for g in L["usgs"]["gauges"]})
    out = {}
    for i in range(0, len(sites), 20):
        ids = ",".join("USGS-" + s for s in sites[i:i + 20])
        url = (f"{USGS_API}/continuous/items?f=json&limit=10000&time=PT{hours}H"
               f"&monitoring_location_id={ids}")
        while url:
            doc = get_json(url)
            for f in doc.get("features", []):
                p = f["properties"]
                try:
                    v = float(p["value"])
                except (TypeError, ValueError):
                    continue
                key = (p["monitoring_location_id"].replace("USGS-", ""), p["parameter_code"], p["time_series_id"])
                out.setdefault(key, []).append((iso(p["time"]), v, p.get("unit_of_measure") or "",
                                                p.get("approval_status") or ""))
            url = next((l["href"] for l in doc.get("links", []) if l.get("rel") == "next"), None)
    return out


def fetch_cwms(office, tsid, begin, end):
    pts, page = [], None
    while True:
        q = {"office": office, "name": tsid, "unit": "EN", "page-size": 5000,
             "begin": begin.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if page:
            q["page"] = page
        doc = get_json(f"{CWMS_API}/timeseries?" + urllib.parse.urlencode(q),
                       {"Accept": "application/json;version=2"})
        vals = doc.get("values") or []
        for v in vals:
            # None, or the sentinels loggers send when out of the water (-99999 in the sensor's unit, seen as
            # -179968 F at the Berlin buoy in October 2026): no lake, flow or weather reading is below -900
            if v[1] is None or v[1] <= -900 or v[1] >= 1e7:
                continue
            t = dt.datetime.fromtimestamp(v[0] / 1000, tz=dt.timezone.utc)
            pts.append((t.isoformat(), round(float(v[1]), 3), doc.get("units") or "", str(v[2]) if len(v) > 2 else ""))
        nxt = doc.get("next-page")
        if not nxt or nxt == page or not vals:
            return pts
        page = nxt


def fetch_metar(stations, hours):
    """{station: [(t, {field: value})]}"""
    url = f"{AWC_API}/metar?" + urllib.parse.urlencode({"ids": ",".join(stations), "format": "json",
                                                          "hours": min(hours, 96)})
    out = {}
    for m in get_json(url) or []:
        t = dt.datetime.fromtimestamp(m["obsTime"], tz=dt.timezone.utc).isoformat()
        vals = {}
        for k, _, _ in WX_FIELDS:
            v = m.get(k)
            if k == "visib" and isinstance(v, str):
                v = v.rstrip("+")
            if k == "wgst" and v is None and m.get("wspd") is not None:
                continue
            try:
                vals[k] = float(v)
            except (TypeError, ValueError):
                if k == "wdir" and v == "VRB":
                    vals["wdir_vrb"] = 1.0
        out.setdefault(m["icaoId"], []).append((t, vals, m.get("rawOb", "")))
    return out


def iso(t):
    return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------- labels

def cwms_label(tsid):
    loc, param = tsid.split(".")[:2]
    side = "Outflow" if "-Outflow" in loc or "Tailwater" in param else "Lake"
    if param == "Elev":
        return "Lake level" if side == "Lake" else "Tailwater level (below the dam)"
    if param == "Flow":
        return "Inflow" if side == "Lake" else "Release (outflow)"
    if param == "Temp-Water":
        return "Water temperature (released water)" if side == "Outflow" else "Water temperature"
    label = CWMS_PARAM.get(param, param)
    return label


def usgs_label(s, gauge):
    note = (s.get("note") or "").strip()
    name = s.get("name") or s["param"]
    if s["param"] in ("62614", "62615"):
        name = "Lake level"
    return f"{name} ({note[0].lower() + note[1:]})" if note else name


# ---------------------------------------------------------------- archive

def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def archive(rows, state):
    """Append rows newer than state to this hour's write-once file; returns the rows actually new."""
    new = []
    for r in rows:
        key = f"{r['lake']}|{r['series']}"
        if r["time_utc"] > state.get(key, ""):
            new.append(r)
    for r in new:
        key = f"{r['lake']}|{r['series']}"
        state[key] = max(state.get(key, ""), r["time_utc"])
    if not new:
        return new
    path = os.path.join(HIST, NOW.strftime("%Y/%m/%d/%H") + ".csv.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = []
    if os.path.exists(path):          # a second run in the same hour adds to the hour's file
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(existing + sorted(new, key=lambda r: (r["lake"], r["series"], r["time_utc"])))
    return new


def recent_rows(days=8):
    """Every archived row from the last `days` days of hourly files."""
    rows = []
    for d in range(days + 1):
        day = NOW - dt.timedelta(days=d)
        folder = os.path.join(HIST, day.strftime("%Y/%m/%d"))
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if name.endswith(".csv.gz"):
                with gzip.open(os.path.join(folder, name), "rt", encoding="utf-8", newline="") as f:
                    rows.extend(csv.DictReader(f))
    return rows


# ---------------------------------------------------------------- public files

def value_at(pts, when):
    """Value of the reading nearest `when` within 90 minutes (pts sorted by time)."""
    best, gap = None, dt.timedelta(minutes=90)
    for t, v in pts:
        d = abs(t - when)
        if d <= gap:
            best, gap = v, d
    return best


def summarize(pts):
    pts = sorted(pts)
    t, v = pts[-1]
    out = {"time": t.isoformat(), "value": v}
    for label, back in (("change_24h", 1), ("change_7d", 7)):
        old = value_at(pts, t - dt.timedelta(days=back))
        if old is not None:
            out[label] = round(v - old, 3)
    hourly = {}
    for pt, pv in pts:
        if pt >= NOW - dt.timedelta(days=7):
            hourly[pt.replace(minute=0, second=0)] = pv
    out["hourly"] = [[k.strftime("%Y-%m-%dT%H:00Z"), hourly[k]] for k in sorted(hourly)]
    out["stale"] = (NOW - t) > dt.timedelta(hours=6)
    return out


# 1 degC per metre, the usual thermocline test, in the buoys' units (degF per foot)
THERMO_F_PER_FT = 1.8 / 3.2808


def build_profile(L, by):
    """The latest temperature-by-depth profile from a lake's buoy, with the thermocline (the depth of the steepest
    drop, if it is at least 1 degC per metre) and a 7-day hourly history of the thermocline depth."""
    series = ((L.get("cwms") or {}).get("profile") or {}).get("series") or []
    if not series:
        return None
    hours = {}
    for s in series:
        for t, v in by.get((L["slug"], f"cwms:{s['tsid']}"), []):
            if not 28 <= v <= 100:   # degF; anything else is a sensor out of the water
                continue
            hours.setdefault(t.replace(minute=0, second=0), {})[s["depth_ft"]] = v
    full = [h for h in sorted(hours) if len(hours[h]) >= max(4, len(series) // 2)]
    if not full:
        return None

    def thermo(prof):
        d = sorted(prof)
        best = None
        for a, b in zip(d, d[1:]):
            g = (prof[a] - prof[b]) / (b - a)
            if g >= THERMO_F_PER_FT and (best is None or g > best[0]):
                best = (g, (a + b) / 2)
        return best[1] if best else None

    last = full[-1]
    prof = hours[last]
    depths = sorted(prof)
    return {"time": last.isoformat(), "stale": (NOW - last) > dt.timedelta(hours=6), "unit": "F",
            "source": f"U.S. Army Corps of Engineers ({L['cwms']['profile']['office']}) water-quality buoy, raw data",
            "points": [[d, round(prof[d], 2)] for d in depths], "thermocline_ft": thermo(prof),
            "surface_f": round(prof[depths[0]], 1), "bottom_f": round(prof[depths[-1]], 1), "bottom_ft": depths[-1],
            "thermocline_hourly": [[h.strftime("%Y-%m-%dT%H:00Z"), thermo(hours[h])] for h in full
                                   if h >= NOW - dt.timedelta(days=7)]}


def build_public(reg, rows, forecasts):
    saved_path = os.path.join(os.path.dirname(STATE), "profiles.json")
    try:
        saved = json.load(open(saved_path, encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    by = {}
    for r in rows:
        try:
            by.setdefault((r["lake"], r["series"]), []).append(
                (dt.datetime.fromisoformat(r["time_utc"]), float(r["value"])))
        except ValueError:
            continue
    now_doc = {"updated_utc": NOW.isoformat(), "lakes": {}}
    os.makedirs(os.path.join(PUB, "lake"), exist_ok=True)
    for L in reg["lakes"]:
        slug = L["slug"]
        groups, head = [], {}
        gauges = {g["site"]: g for g in L["usgs"]["gauges"]}
        # USGS, one group per gauge
        for site, g in gauges.items():
            items = []
            for s in L["usgs"]["series"]:
                if s["site"] != site:
                    continue
                key = f"usgs:{site}:{s['param']}:{s['time_series_id']}"
                pts = by.get((slug, key))
                if not pts:
                    continue
                item = {"key": key, "param": s["param"], "label": usgs_label(s, g), "unit": s["unit"]}
                item.update(summarize(pts))
                items.append(item)
                role = g.get("role")
                if s["param"] in ("62614", "62615") and role == "lake" and "level" not in head:
                    head["level"] = dict(item, source="USGS", site=site,
                                         datum="NGVD 1929" if s["param"] == "62614" else "NAVD 1988")
                if s["param"] == "00010" and role in ("lake", "below_dam"):
                    where = "in the lake" if role == "lake" else "in the river below the dam"
                    if "water_temp" not in head or (role == "lake" and head["water_temp"]["where"] != "in the lake"):
                        head["water_temp"] = dict(item, source="USGS", site=site, where=where)
                if s["param"] == "00065" and role == "lake" and "level" not in head and "gage" not in head:
                    head["gage"] = dict(item, source="USGS", site=site)
                if s["param"] in ("00300", "32315", "32316", "32319", "32283", "63680") and role == "lake":
                    head.setdefault("quality", []).append({"label": item["label"], "unit": item["unit"],
                                                           "value": item["value"], "time": item["time"]})
            if items:
                groups.append({"source": "USGS", "id": site, "name": g.get("name") or site, "role": g.get("role"),
                               "url": f"https://waterdata.usgs.gov/monitoring-location/USGS-{site}/",
                               "series": items})
        # USACE
        cw = L.get("cwms") or {}
        items = []
        for s in cw.get("series", []):
            tsid = s["tsid"]
            if "FCST" in tsid or tsid.endswith(".RUL"):
                continue
            key = f"cwms:{tsid}"
            pts = by.get((slug, key))
            if not pts:
                continue
            item = {"key": key, "label": cwms_label(tsid), "unit": s.get("unit") or ""}
            item.update(summarize(pts))
            items.append(item)
            label = item["label"]
            if label == "Lake level":
                head["level"] = dict(item, source="USACE", datum="NGVD 1929")   # the Corps' pool reading leads
            elif label == "Storage":
                head["storage"] = dict(item, source="USACE")
            elif label == "Inflow":
                head["inflow"] = dict(item, source="USACE")
            elif label == "Release (outflow)":
                head["release"] = dict(item, source="USACE")
            elif label.startswith("Water temperature") and head.get("water_temp", {}).get("where") != "in the lake":
                head["water_temp"] = dict(item, source="USACE", where="released at the dam")
        if items:
            groups.append({"source": "USACE", "id": cw.get("office"), "name": f"U.S. Army Corps of Engineers ({cw.get('office')})",
                           "url": "https://water.usace.army.mil/", "series": items})
        # weather
        for w in L.get("weather", [])[:1]:
            items = []
            for k, (label, unit) in WX_LABEL.items():
                pts = by.get((slug, f"wx:{w['station']}:{k}"))
                if pts:
                    item = {"key": f"wx:{w['station']}:{k}", "label": label, "unit": unit}
                    item.update(summarize(pts))
                    items.append(item)
            if items:
                groups.append({"source": "NWS", "id": w["station"], "name": f"{w['name']} weather station ({w['station']}), {w['km']} km away",
                               "url": f"https://aviationweather.gov/data/metar/?ids={w['station']}", "series": items})
                wx = {i["key"].split(":")[-1]: i for i in items}
                head["wind"] = {"station": w["station"], "km": w["km"], "time": items[0]["time"],
                                "speed_kt": wx.get("wspd", {}).get("value"), "gust_kt": wx.get("wgst", {}).get("value"),
                                "dir_deg": wx.get("wdir", {}).get("value")}
                if "temp" in wx:
                    head["air_temp"] = {"value": wx["temp"]["value"], "unit": "degC", "time": wx["temp"]["time"]}
        profile = build_profile(L, by)
        if profile:
            saved[slug] = dict(profile, thermocline_hourly=[])
        elif slug in saved:   # the buoy is out of the water: keep showing when it last reported
            profile = dict(saved[slug], stale=True)
        if profile and not profile["stale"]:
            head["thermocline"] = {k: profile[k] for k in ("time", "thermocline_ft", "surface_f", "bottom_f", "bottom_ft")}
        doc = {"slug": slug, "name": L["name"], "updated_utc": NOW.isoformat(), "operator": L.get("operator"),
               "headline": head, "groups": groups, "forecast": forecasts.get(slug, {}), "profile": profile,
               "note": "Provisional data, mirrored hourly from USGS, the U.S. Army Corps of Engineers and the "
                       "National Weather Service. Recent readings may be revised by the agencies."}
        with open(os.path.join(PUB, "lake", f"{slug}.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        slim = {k: ({kk: vv for kk, vv in v.items() if kk != "hourly"} if isinstance(v, dict) else v)
                for k, v in head.items()}
        now_doc["lakes"][slug] = {"name": L["name"], **slim}
    with open(os.path.join(PUB, "now.json"), "w", encoding="utf-8") as f:
        json.dump(now_doc, f, separators=(",", ":"))
    os.makedirs(os.path.dirname(saved_path), exist_ok=True)
    with open(saved_path, "w", encoding="utf-8") as f:
        json.dump(saved, f, separators=(",", ":"))


# ---------------------------------------------------------------- upload

def upload(mode):
    files = [os.path.join(dp, n) for dp, _, ns in os.walk(PUB) for n in ns]
    if mode == "rclone":
        subprocess.run(["rclone", "--config", os.path.expanduser("~/.config/rclone/rclone.conf"),
                        "--s3-no-check-bucket", "--header-upload", "Cache-Control: public, max-age=300",
                        "copy", PUB, "r2:crackedbuckeye/lakes-live"], check=True)
    elif mode == "s3":
        import boto3
        s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"], region_name="auto",
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
        for p in files:
            key = "lakes-live/" + os.path.relpath(p, PUB).replace(os.sep, "/")
            s3.upload_file(p, "crackedbuckeye", key, ExtraArgs={"ContentType": "application/json",
                                                                 "CacheControl": "public, max-age=300"})
    print(f"uploaded {len(files)} files to r2:crackedbuckeye/lakes-live")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--upload", choices=["rclone", "s3"])
    args = ap.parse_args()
    reg = json.load(open(os.path.join(HERE, "registry.json"), encoding="utf-8"))
    state = load_state()

    def lookback(src):
        """Hours to reach back for one source: the default, or past the gap since that source last answered (a
        skipped GitHub run, a rate-limited hour, an agency outage), up to a week."""
        last = state.get(f"_ok_{src}")
        if not last:   # never answered yet: keep asking for the whole week until it does
            return max(args.hours, 168) if state.get("_last_run") else args.hours
        gap = (NOW - dt.datetime.fromisoformat(last)).total_seconds() / 3600
        return min(max(args.hours, 168), max(args.hours, int(gap) + 3))
    hours = {src: lookback(src) for src in ("usgs", "cwms", "nws")}
    begin = NOW - dt.timedelta(hours=hours["cwms"])
    rows, forecasts, errors = [], {}, []

    try:
        usgs = fetch_usgs(reg, hours["usgs"])
        state["_ok_usgs"] = NOW.isoformat()
    except Exception as e:
        usgs, _ = {}, errors.append(f"USGS: {e}")
    site_lakes = {}
    for L in reg["lakes"]:
        for s in L["usgs"]["series"]:
            site_lakes.setdefault((s["site"], s["param"], s["time_series_id"]), []).append(L["slug"])
    for (site, param, tsid), pts in usgs.items():
        for slug in site_lakes.get((site, param, tsid), []):   # unregistered series wait for build_registry.py
            for t, v, unit, flag in pts:
                rows.append({"time_utc": t, "lake": slug, "source": "usgs",
                             "series": f"usgs:{site}:{param}:{tsid}", "value": v, "unit": unit, "flag": flag})

    # The Corps' API serves one series per request; eight at a time keeps a run near a minute.
    jobs = [(L["slug"], (L.get("cwms") or {}).get("office"), s["tsid"]) for L in reg["lakes"]
            for s in (L.get("cwms") or {}).get("series", [])]
    # seasonal depth buoys: empty answers out of season, so they cost little and resume by themselves
    jobs += [(L["slug"], L["cwms"]["profile"]["office"], s["tsid"]) for L in reg["lakes"]
             if (L.get("cwms") or {}).get("profile") for s in L["cwms"]["profile"]["series"]]

    def one(job):
        slug, office, tsid = job
        if "FCST" in tsid or tsid.endswith(".RUL"):
            return job, fetch_cwms(office, tsid, NOW - dt.timedelta(days=1), NOW + dt.timedelta(days=30))
        return job, fetch_cwms(office, tsid, begin, NOW)

    cwms_failed = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, j) for j in jobs]
        for fut, job in zip(futures, jobs):
            slug, office, tsid = job
            try:
                _, pts = fut.result()
            except Exception as e:
                errors.append(f"CWMS {tsid}: {e}")
                cwms_failed += 1
                continue
            if "FCST" in tsid or tsid.endswith(".RUL"):
                if pts:
                    forecasts.setdefault(slug, {})[tsid] = {
                        "label": ("Corps forecast: " if "FCST" in tsid else "Guide curve: ") + cwms_label(tsid),
                        "unit": pts[0][2], "points": [[t, v] for t, v, _, _ in pts if t >= NOW.isoformat()][:240]}
                continue
            for t, v, unit, flag in pts:
                rows.append({"time_utc": t, "lake": slug, "source": "cwms", "series": f"cwms:{tsid}",
                             "value": v, "unit": unit, "flag": flag})

    if cwms_failed * 2 < max(1, len(jobs)):
        state["_ok_cwms"] = NOW.isoformat()

    stations = sorted({w["station"] for L in reg["lakes"] for w in L.get("weather", [])[:1]})
    try:
        metar = fetch_metar(stations, hours["nws"])
        state["_ok_nws"] = NOW.isoformat()
    except Exception as e:
        metar, _ = {}, errors.append(f"METAR: {e}")
    for L in reg["lakes"]:
        for w in L.get("weather", [])[:1]:
            for t, vals, raw in metar.get(w["station"], []):
                for k, v in vals.items():
                    if k in WX_LABEL:
                        rows.append({"time_utc": t, "lake": L["slug"], "source": "nws",
                                     "series": f"wx:{w['station']}:{k}", "value": v, "unit": WX_LABEL[k][1],
                                     "flag": ""})

    new = archive(rows, state)
    state["_last_run"] = NOW.isoformat()
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0, sort_keys=True)
    build_public(reg, recent_rows(), forecasts)
    with open(os.path.join(PUB, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"run_utc": NOW.isoformat(), "lookback_hours": hours, "fetched": len(rows), "new": len(new),
                   "errors": errors[:50]}, f, indent=1)
    print(f"{NOW.isoformat()}: looked back {hours}; fetched {len(rows)} readings, {len(new)} new, {len(errors)} errors")
    for e in errors[:20]:
        print("  ", e)
    if args.upload:
        upload(args.upload)
    # A total outage of every source is a failure worth an email from CI; a single dead gauge is not.
    if not rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
