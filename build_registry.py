"""build_registry.py -- registry.json: every live data series for 34 Ohio lakes.

    python build_registry.py [--atlas C:/Users/jrcoo/work/ohio-tap-water-atlas]
                             [--lakes C:/Users/jrcoo/work/ohio-tap-water-atlas-redesign/data/site/lakes.json]

Run it by hand when a lake, a gauge or a Corps series is added; the hourly job (fetch.py) only reads the result.

Where the series come from:
  * USGS gauges: the ones each lake deep dive already chose (data/<slug>/live/fetch_live.py in the atlas repo),
    expanded to every continuous series USGS publishes at those gauges (time-series-metadata, statistic 00011).
  * USACE CWMS: the Corps location named in those scripts, expanded through the CWMS catalog to every public
    observed series, rule curve and forecast for that location that has reported in the last 14 days.
  * Weather (wind, air temperature, gusts, pressure): the nearest METAR station (aviationweather.gov) to the lake.
  * Lake Erie (slug lakeerie, added 2026-10-10): not one of those 34; its stations are the lists ERIE_COOPS / ERIE_NDBC
    below, verified live (which products each CO-OPS gauge carries, which columns each NDBC station sends), and the
    NWS nearshore forecast files. `--erie-only` refreshes just that entry.
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import erie

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "CrackedBuckeye/1.0 (crackedbuckeye.com; Ohio lake conditions)"}
USGS_API = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
CWMS_API = "https://cwms-data.usace.army.mil/cwms-data"
AWC_API = "https://aviationweather.gov/api/data"

USGS_RE = re.compile(r"""["'](?:USGS-)?(\d{8}|\d{15})["']""")
CWMS_RE = re.compile(r"""["']([A-Za-z][\w-]*\.[A-Za-z-]+\.(?:Inst|Ave|Total|Max|Min)\.\w+\.\w+\.[\w-]+)["']""")
OFFICE_RE = re.compile(r"""office\W{1,4}(LR[A-Z])""", re.I)
# Versions worth publishing; '-Test' and other working copies are skipped.
CWMS_KEEP = re.compile(r"\.(OBS|RUL|lrldlb-(comp|raw|rev)|CELR[A-Z]-FCST-DAILY)$")

# Corps locations the deep-dive scripts did not name (they read these pools from USGS instead).
CWMS_EXTRA = {"piedmont": ("LRH", ["Piedmont"]), "seneca": ("LRH", ["Senecaville"])}
# Gate openings, per-gate flows, alternate sensors and first-guess inflows are working data, not conditions.
CWMS_SKIP = re.compile(r"\.(Opening-[^.]+|Flow-(BP|MG|L)\d|Flow-Alt|Stage-Alt|Flow-Inflow-Initial)\.")
VERSION_RANK = ["OBS", "lrldlb-comp", "lrldlb-rev", "lrldlb-raw"]
INTERVAL_RANK = ["15Minutes", "1Hour", "0", "~1Day", "1Day"]

# USGS gauges the deep-dive scripts left out.
EXTRA_GAUGES = {"oshaughnessy": ["03220500"]}
GENERIC = {"lake", "lakes", "reservoir", "the", "of", "creek", "fork", "j.", "st."}

# Seasonal water-quality buoys (CWMS "WQDataLive"): temperature every few feet down, in the water roughly June to
# September. Kept without the 14-day recency test so they come back by themselves each summer. The 0 ft node and the
# EXO2 sonde read 10-20 degC off their neighbours in 2026 (data/lakes/thermocline/README.md) and are left out.
BUOYS = {"berlin": ("LRP", "Berlin-Lake-D"), "westbranch": ("LRP", "Kirwan-Lake-D")}
BUOY_SKIP = re.compile(r"-D0+ft\.|EXO2")

# Pymatuning has no NID point in the site data (its dam is in Pennsylvania); centre of the lake.
FALLBACK_POINT = {"pymatuning": (41.556, -80.497)}



def usgs_key():
    """The api.data.gov key: USGS_API_KEY (the GitHub secret in CI), else API_DATA_GOV_KEY, the owner's key on JoelHome
    (set 2026-10-10 as a Windows user variable; read from the registry when this shell started before it was set)."""
    k = os.environ.get("USGS_API_KEY") or os.environ.get("API_DATA_GOV_KEY")
    if not k and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as r:
                k = winreg.QueryValueEx(r, "API_DATA_GOV_KEY")[0]
        except OSError:
            k = None
    k = (k or '').strip().lstrip('﻿').strip()   # a key piped from Windows PowerShell can carry a BOM (10-10)
    return k or None

def get_json(url, headers=None, insecure=False, tries=3):
    h = dict(UA)
    h.update(headers or {})
    key = usgs_key()
    if "api.waterdata.usgs.gov" in url and key:
        h["X-Api-Key"] = key   # free api.data.gov key: a higher hourly limit than anonymous
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90, context=ctx) as r:
                return json.load(r)
        except ssl.SSLError:
            if insecure:
                raise
            insecure = True
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(30 * (i + 1))


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(x))


def gauge_role(name, typ):
    """lake (reads the pool), below_dam (the river just below the dam), inflow (a stream above the lake), or the
    USGS site type (stream, well, atmosphere, canal)."""
    n = name.lower()
    if typ.startswith("Lake"):
        return "lake"
    if re.search(r"\b(bl|below)\b.*\b(dam|reservoir|lake)\b|\bat .*\bdam\b", n):
        return "below_dam"
    if re.search(r"\babove\b.*\b(lake|reservoir)\b", n):
        return "inflow"
    return (typ or "other").split(",")[0].lower()


def usgs_series(site_ids):
    """Every continuous (statistic 00011) series at these gauges that has reported in the last 30 days."""
    ids = ",".join("USGS-" + s for s in site_ids)
    names = {}
    doc = get_json(f"{USGS_API}/monitoring-locations/items?f=json&limit=500&id={ids}")
    for f in doc["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2] if f.get("geometry") else (None, None)
        names[f["id"].replace("USGS-", "")] = (p.get("monitoring_location_name"), p.get("site_type"), lat, lon)
    out = []
    url = f"{USGS_API}/time-series-metadata/items?f=json&limit=1000&monitoring_location_id={ids}"
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    while url:
        doc = get_json(url)
        for f in doc["features"]:
            p = f["properties"]
            if p.get("statistic_id") != "00011":
                continue
            end = p.get("end_utc") or p.get("end")
            try:
                end_t = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
                if end_t.tzinfo is None:
                    end_t = end_t.replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if end_t < cutoff:
                continue
            out.append({"time_series_id": f["id"], "site": p["monitoring_location_id"].replace("USGS-", ""),
                        "param": p["parameter_code"], "name": p.get("parameter_name"),
                        "unit": p.get("unit_of_measure"), "note": p.get("web_description") or ""})
        url = next((l["href"] for l in doc.get("links", []) if l.get("rel") == "next"), None)
    return names, out


def pick_best(names):
    """One series per location.parameter.type: the observed version at the finest interval. Forecasts and rule
    curves are their own kinds and always kept."""
    best = {}
    for n in names:
        loc, param, typ, interval, dur, ver = n.split(".", 5)
        if "FCST" in ver or ver == "RUL":
            best[n] = (0, 0, n)
            continue
        key = (loc, param, typ)
        rank = (VERSION_RANK.index(ver) if ver in VERSION_RANK else 9,
                INTERVAL_RANK.index(interval) if interval in INTERVAL_RANK else 9)
        if key not in best or rank < best[key][:2]:
            best[key] = rank + (n,)
    return sorted(v[2] for v in best.values())


def cwms_series(office, prefixes):
    """Catalogued CWMS series at these locations, kept if they reported in the last 14 days."""
    found = []
    for pre in prefixes:
        like = "^" + re.escape(pre) + r"[.-].*"
        url = f"{CWMS_API}/catalog/TIMESERIES?" + urllib.parse.urlencode({"office": office, "like": like, "page-size": 500})
        doc = get_json(url, {"Accept": "application/json;version=2"})
        for e in doc.get("entries", []):
            if CWMS_KEEP.search(e["name"]) and not CWMS_SKIP.search(e["name"]) and e["name"] not in found:
                found.append(e["name"])
    found = pick_best(found)
    end = dt.datetime.now(dt.timezone.utc)
    begin = end - dt.timedelta(days=14)

    def check(name):
        q = urllib.parse.urlencode({"office": office, "name": name, "unit": "EN", "page-size": 5,
                                    "begin": begin.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "end": (end + dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        try:
            doc = get_json(f"{CWMS_API}/timeseries?{q}", {"Accept": "application/json;version=2"}, tries=2)
        except Exception as e:
            print(f"  CWMS {name}: {e}")
            return None
        if any(v[1] is not None for v in doc.get("values", [])):
            return {"tsid": name, "unit": doc.get("units")}
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        live = [x for x in pool.map(check, found) if x]
    return live


def buoy_series(office, prefix):
    like = "^" + re.escape(prefix) + r"[0-9]+ft\.Temp\..*"
    doc = get_json(f"{CWMS_API}/catalog/TIMESERIES?" + urllib.parse.urlencode({"office": office, "like": like, "page-size": 500}),
                   {"Accept": "application/json;version=2"})
    out = []
    for e in doc.get("entries", []):
        n = e["name"]
        m = re.search(r"-D(\d+)ft\.", n)
        if m and not BUOY_SKIP.search(n):
            out.append({"tsid": n, "depth_ft": int(m.group(1))})
    return sorted(out, key=lambda x: x["depth_ft"])


def add_buoys(lakes):
    for L in lakes:
        if L["slug"] in BUOYS:
            office, prefix = BUOYS[L["slug"]]
            L.setdefault("cwms", None)
            if not L["cwms"]:
                L["cwms"] = {"office": office, "series": []}
            L["cwms"]["profile"] = {"office": office, "series": buoy_series(office, prefix)}
            print(f"{L['slug']}: buoy with {len(L['cwms']['profile']['series'])} depths")


# ---------------------------------------------------------------- Lake Erie (Ohio waters)

# NOAA CO-OPS water-level gauges on the Ohio shore (Erie, PA and Fermi, MI are outside Ohio).
ERIE_COOPS = ["9063085", "9063079", "9063063", "9063053"]
COOPS_CANDIDATES = ["water_level", "water_temperature", "air_temperature", "wind", "air_pressure", "humidity"]
# NDBC buoys and shore stations in Ohio waters, with the name this site uses. Left out on purpose: the four NOS gauges
# (cndo1, faio1, mrho1, thro1 -- the CO-OPS gauges above, read directly), the Old Woman Creek estuary stations (not the
# lake), Toledo Light No. 2 (thlo1; silent since 15 September), and Camp Perry, Geneva-on-the-Lake and Lorain Harbor
# (no realtime file).
ERIE_NDBC = {
    "twco1": "Toledo Crib", "45165": "Toledo Water Intake buoy", "45200": "Maumee Bay buoy",
    "45005": "West Erie buoy (16 nm NW of Lorain)", "sbio1": "South Bass Island", "45201": "Erie Islands buoy",
    "45202": "Port Clinton buoy", "45203": "Huron buoy", "hhlo1": "Huron Harbor Light", "vrmo1": "Vermilion River",
    "45204": "Sheffield buoy", "45196": "Rocky River buoy", "45205": "Edgewater Beach buoy",
    "45176": "Cleveland Intake Crib buoy", "45206": "Euclid Beach buoy", "45197": "Euclid buoy",
    "45164": "Cleveland buoy", "45207": "Mentor Harbor buoy", "45208": "Ashtabula buoy", "asbo1": "Ashtabula Lighthouse",
    "cblo1": "Conneaut Breakwater Light",
}
OWNER_NAME = {"Limno Tech": "LimnoTech", "NWS WFO Cleveland, OH (CLE)": "NWS Cleveland"}
ERIE_MARINE = ["lez142", "lez144", "lez147"]      # each file covers its neighbours: 142-143, 144-146, 147-149
ERIE_COUNTIES = ["Lucas", "Ottawa", "Sandusky", "Erie", "Lorain", "Cuyahoga", "Lake", "Ashtabula"]


def get_text(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(10 * (i + 1))


def erie_entry(prev=None):
    """The registry entry for Lake Erie, verified against the live services. `prev` (the previous entry) fills in
    anything a failed probe could not confirm."""
    prev = prev or {}
    old = prev.get("erie", {})
    now = dt.datetime.now(dt.timezone.utc)
    coops = []
    for sid in ERIE_COOPS:
        before = next((c for c in old.get("coops", []) if c["id"] == sid), {})
        try:
            md = get_json(f"{erie.COOPS_MD}/stations/{sid}.json")["stations"][0]
            name, lat, lon = md["name"], float(md["lat"]), float(md["lng"])
        except Exception as e:
            print(f"   CO-OPS {sid}: metadata failed ({e}); keeping the previous entry")
            if before:
                coops.append(before)
            continue
        have = []
        for product in COOPS_CANDIDATES:
            q = {"product": product, "application": "CrackedBuckeye", "station": sid, "time_zone": "gmt",
                 "units": "metric", "format": "json", "datum": "IGLD",
                 "begin_date": (now - dt.timedelta(hours=6)).strftime("%Y%m%d %H:%M"), "end_date": now.strftime("%Y%m%d %H:%M")}
            try:
                doc = get_json(erie.COOPS_API + "?" + urllib.parse.urlencode(q), tries=2)
                if doc.get("data"):
                    have.append(product)
            except Exception as e:
                print(f"   CO-OPS {sid} {product}: {e}")
                if product in before.get("products", []):
                    have.append(product)
        coops.append({"id": sid, "name": name, "lat": lat, "lon": lon, "products": have or before.get("products", [])})
        print(f"   CO-OPS {sid} {name}: {have}")
    lwd = old.get("lwd_ft")
    try:
        d = get_json(f"{erie.COOPS_MD}/stations/9063063/datums.json?units=english")
        lwd = next(x["value"] for x in d["datums"] if x["name"] == "GL_LWD")
    except Exception as e:
        print(f"   LWD datum: {e}")

    meta = {}
    try:
        active = get_text(f"{erie.NDBC}/activestations.xml")
        for m in re.finditer(r"<station ([^>]*?)/?>", active):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            meta[a["id"].lower()] = a
    except Exception as e:
        print(f"   NDBC station list failed ({e})")
    ndbc = []
    for sid, short in ERIE_NDBC.items():
        before = next((c for c in old.get("ndbc", []) if c["id"] == sid), {})
        a = meta.get(sid)
        if not a and not before:
            print(f"   NDBC {sid}: not in the active-stations list; left out")
            continue
        try:
            text = get_text(erie.ndbc_url(sid, 24))
            rows = erie.parse_ndbc(text, sid, now - dt.timedelta(days=5))
            fields = sorted({r["series"].split(":")[2] for r in rows}, key=erie.FIELD_ORDER.index)
        except Exception as e:
            print(f"   NDBC {sid}: {e}")
            fields = before.get("fields", [])
        owner = (a or {}).get("owner") or before.get("owner") or "NOAA NDBC"
        ndbc.append({"id": sid, "name": short, "owner": OWNER_NAME.get(owner, owner),
                     "kind": "buoy" if sid.isdigit() else "shore station",
                     "lat": float(a["lat"]) if a else before["lat"], "lon": float(a["lon"]) if a else before["lon"],
                     "fields": fields})
        print(f"   NDBC {sid} {short}: {fields}")
    marine = []
    for f in ERIE_MARINE:
        try:
            parsed = erie.parse_nsh(get_text(erie.NWS_TEXT + f + ".txt"), f)
            marine.append({"file": f, "zones": parsed["ids"], "names": parsed["names"]})
        except Exception as e:
            print(f"   NWS {f}: {e}")
            marine.extend(m for m in old.get("marine", []) if m["file"] == f)
        print(f"   NWS {f}: {marine[-1]['zones'] if marine else None}")
    return {"slug": erie.SLUG, "name": "Lake Erie (Ohio shore)", "kind": "great_lake", "lat": 41.62, "lon": -82.45,
            "operator": "NOAA", "counties": ERIE_COUNTIES,
            "usgs": {"gauges": [], "series": []}, "cwms": None, "weather": [],
            "erie": {"lwd_ft": lwd, "coops": coops, "ndbc": ndbc, "marine": marine}}


def add_erie(lakes, prev=None):
    """Replace or append the Lake Erie entry (after the inland lakes, which stay sorted by slug)."""
    lakes[:] = [L for L in lakes if L["slug"] != erie.SLUG]
    lakes.append(erie_entry(prev))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--atlas", default="C:/Users/jrcoo/work/ohio-tap-water-atlas")
    ap.add_argument("--lakes", default="C:/Users/jrcoo/work/ohio-tap-water-atlas-redesign/data/site/lakes.json")
    ap.add_argument("--buoys-only", action="store_true", help="refresh only the buoy lists in registry.json")
    ap.add_argument("--erie-only", action="store_true", help="refresh only the Lake Erie entry in registry.json")
    args = ap.parse_args()
    if args.buoys_only:
        path = os.path.join(HERE, "registry.json")
        reg = json.load(open(path, encoding="utf-8"))
        add_buoys(reg["lakes"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1)
        return

    if args.erie_only:
        path = os.path.join(HERE, "registry.json")
        reg = json.load(open(path, encoding="utf-8"))
        add_erie(reg["lakes"], next((L for L in reg["lakes"] if L["slug"] == erie.SLUG), None))
        reg["built_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1)
        return

    site_lakes = json.load(open(args.lakes, encoding="utf-8"))
    site_lakes = site_lakes.get("lakes", site_lakes) if isinstance(site_lakes, dict) else site_lakes
    stations = [s for s in get_json(f"{AWC_API}/stationinfo?bbox=38.0,-85.5,42.5,-79.8&format=json")
                if s.get("icaoId") and "METAR" in (s.get("siteType") or [])]

    try:
        prev = {L["slug"]: L for L in json.load(open(os.path.join(HERE, "registry.json"), encoding="utf-8"))["lakes"]}
    except (OSError, ValueError, KeyError):
        prev = {}
    lakes, usgs_down = [], False
    for L in sorted(site_lakes, key=lambda x: x["slug"]):
        slug = L["slug"]
        point = (L.get("lat"), L.get("lon")) if L.get("lat") else FALLBACK_POINT[slug]
        src = os.path.join(args.atlas, "data", slug, "live", "fetch_live.py")
        text = open(src, encoding="utf-8").read() if os.path.exists(src) else ""
        sites = sorted(set(USGS_RE.findall(text)) | set(EXTRA_GAUGES.get(slug, [])))
        tsids = sorted(set(CWMS_RE.findall(text)))
        offices = sorted(set(o.upper() for o in OFFICE_RE.findall(text)))
        print(f"{slug}: {len(sites)} USGS gauges, {len(tsids)} named CWMS series, office {offices}")

        try:
            if usgs_down and sites:
                raise RuntimeError("skipped after an earlier refusal")
            names, series = usgs_series(sites) if sites else ({}, [])
        except Exception as e:
            usgs_down = True
            # USGS rate-limits by address (1,000 requests an hour, shared with every other job on the machine).
            # Keep this lake's gauges from the last registry rather than lose them.
            old = prev.get(slug, {}).get("usgs", {})
            names = {g["site"]: (g.get("name"), g.get("type"), g.get("lat"), g.get("lon")) for g in old.get("gauges", [])}
            series = old.get("series", [])
            print(f"   USGS unavailable ({e}); kept {len(series)} series from the previous registry")
        gauges = []
        own = [w for w in re.findall(r"[a-z.']+", L["name"].lower()) if w not in GENERIC]
        for s in sites:
            name, typ, lat, lon = names.get(s) or (None, None, None, None)
            role = gauge_role(name or "", typ or "")
            # a pool gauge on a different lake (Salt Fork's script read Senecaville Lake for context) is not this lake's
            if role == "lake" and not any(w.rstrip(".") in (name or "").lower() for w in own):
                print(f"   skip {s} {name}: another lake's pool gauge")
                continue
            gauges.append({"site": s, "name": name, "type": typ, "role": role, "lat": lat, "lon": lon})
        series = [x for x in series if x["site"] in {g["site"] for g in gauges}]

        cwms = None
        if tsids and offices:
            prefixes = sorted({t.split(".")[0].split("-")[0] for t in tsids})
            cwms = {"office": offices[0], "series": cwms_series(offices[0], prefixes)}
        elif slug in CWMS_EXTRA:
            office, prefixes = CWMS_EXTRA[slug]
            cwms = {"office": office, "series": cwms_series(office, prefixes)}

        near = sorted(stations, key=lambda s: km(point, (s["lat"], s["lon"])))[:2]
        weather = [{"station": s["icaoId"], "name": s["site"], "km": round(km(point, (s["lat"], s["lon"])), 1)}
                   for s in near]

        lakes.append({"slug": slug, "name": L["name"], "lat": point[0], "lon": point[1],
                      "operator": L.get("operator"), "counties": L.get("counties"),
                      "usgs": {"gauges": gauges, "series": series}, "cwms": cwms, "weather": weather})
        print(f"   -> {len(series)} USGS series, {len(cwms['series']) if cwms else 0} CWMS series, "
              f"weather {weather[0]['station']} {weather[0]['km']} km")

    add_buoys(lakes)
    add_erie(lakes, prev.get(erie.SLUG))
    reg = {"built_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "sources": {"usgs": USGS_API, "cwms": CWMS_API, "weather": AWC_API,
                       "erie": {"coops": erie.COOPS_API, "ndbc": erie.NDBC + "/data/5day2/", "marine": erie.NWS_TEXT}},
           "lakes": lakes}
    with open(os.path.join(HERE, "registry.json"), "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1)
    print("wrote registry.json:", len(lakes), "lakes")


if __name__ == "__main__":
    main()
