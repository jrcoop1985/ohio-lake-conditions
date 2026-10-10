"""erie.py -- Lake Erie (Ohio waters) for the hourly feed. Owner's ask, 2026-10-10: "can we include lake erie conditions?"

Public NOAA sources, no key, nothing but a generic User-Agent:

  CO-OPS  https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
          Toledo 9063085, Marblehead 9063079, Cleveland 9063063, Fairport 9063053: water level (IGLD 1985), water
          temperature, wind, air temperature, pressure, humidity -- whichever each gauge carries (the registry lists them).
  NDBC    https://www.ndbc.noaa.gov/data/5day2/<ID>_5day.txt   (realtime2/<ID>.txt for a longer look-back)
          The buoys and shore stations in Ohio waters, run by NDBC, LimnoTech and the Cleveland Water Alliance and relayed
          by NDBC: wind, waves, water and air temperature, pressure. Many are pulled out of the water in fall and winter.
  NWS     https://tgftp.nws.noaa.gov/data/forecasts/marine/near_shore/le/lez142.txt (144, 147)
          The Nearshore Marine Forecast for zones LEZ142-LEZ149 (Maumee Bay to Conneaut), a text product of the
          Cleveland forecast office. api.weather.gov would be tidier but its robots.txt disallows every path.

fetch.py calls fetch_coops / fetch_ndbc / fetch_marine and, when it builds the public files, build(). Archive rows use the
agencies' own units (metres, degC, m/s, hPa); build() converts to what the site shows (feet, knots).

Honest about gaps: a buoy that is out of the water simply stops reporting. Its last reading stays in the archive, build()
marks its readings stale after 6 hours and the station "not reporting" after 3, and says when it last reported (from
state/last_seen.json, so that still works weeks after the 8-day window of recent readings has moved on).
"""
import datetime as dt
import re
import urllib.parse

COOPS_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
COOPS_MD = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
NDBC = "https://www.ndbc.noaa.gov"
NWS_TEXT = "https://tgftp.nws.noaa.gov/data/forecasts/marine/near_shore/le/"
SLUG = "lakeerie"
UTC = dt.timezone.utc

# field: (label, unit as the agency publishes it, unit shown, factor)
FIELDS = {
    "level": ("Water level (IGLD 1985)", "m", "ft", 3.28084),
    "water_temp": ("Water temperature", "degC", "degC", 1.0),
    "air_temp": ("Air temperature", "degC", "degC", 1.0),
    "wind_speed": ("Wind speed", "m/s", "kt", 1.943844),
    "wind_gust": ("Wind gust", "m/s", "kt", 1.943844),
    "wind_dir": ("Wind direction (from)", "deg", "deg", 1.0),
    "wave_height": ("Wave height", "m", "ft", 3.28084),
    "wave_period": ("Wave period", "s", "s", 1.0),
    "pressure": ("Pressure", "hPa", "hPa", 1.0),
    "humidity": ("Relative humidity", "%", "%", 1.0),
}
FIELD_ORDER = ["level", "water_temp", "air_temp", "wind_speed", "wind_gust", "wind_dir", "wave_height", "wave_period",
               "pressure", "humidity"]
# a value outside these (in the agency's unit) is a sensor fault or a sentinel, never weather
PLAUSIBLE = {"level": (170, 180), "water_temp": (-2, 35), "air_temp": (-40, 50), "wind_speed": (0, 70),
             "wind_gust": (0, 90), "wind_dir": (0, 360), "wave_height": (0, 12), "wave_period": (0, 30),
             "pressure": (900, 1100), "humidity": (0, 100)}

# CO-OPS product -> [(key in the answer, field)]
COOPS_PRODUCTS = {
    "water_level": [("v", "level")],
    "water_temperature": [("v", "water_temp")],
    "air_temperature": [("v", "air_temp")],
    "wind": [("s", "wind_speed"), ("g", "wind_gust"), ("d", "wind_dir")],
    "air_pressure": [("v", "pressure")],
    "humidity": [("v", "humidity")],
}
# NDBC column -> field
NDBC_COLUMNS = {"WSPD": "wind_speed", "GST": "wind_gust", "WDIR": "wind_dir", "WVHT": "wave_height",
                "DPD": "wave_period", "WTMP": "water_temp", "ATMP": "air_temp", "PRES": "pressure"}

# Only these get a 7-day hourly trace in lake/lakeerie.json (value decimals), and as {"t0", "v": [...]} -- one value per
# hour from t0, null for an hour with no reading -- not the [[time, value], ...] pairs the inland lakes use: the page
# downloads this file whole and 25 stations at 10-minute steps would make it 700 KB. The archive keeps every reading.
TRACE = {"level": 2, "water_temp": 1, "wave_height": 1, "wind_speed": 1}


def _trace(hourly, nd):
    """[[ '2026-10-03T18:00Z', v ], ...] (hours with a reading, in order) -> {'t0': first hour, 'v': [v | None per hour]}."""
    if not hourly:
        return None
    fmt = "%Y-%m-%dT%H:00Z"
    t0 = dt.datetime.strptime(hourly[0][0], fmt)
    n = int((dt.datetime.strptime(hourly[-1][0], fmt) - t0).total_seconds() // 3600) + 1
    v = [None] * n
    for t, val in hourly:
        v[int((dt.datetime.strptime(t, fmt) - t0).total_seconds() // 3600)] = round(val, nd)
    return {"t0": hourly[0][0], "v": v}

FRESH = dt.timedelta(hours=3)      # a station that has not reported in this long is "not reporting"
STALE = dt.timedelta(hours=6)      # same threshold fetch.summarize uses for a series


def _iso(t):
    return t.astimezone(UTC).isoformat()


def _ok(field, v):
    lo, hi = PLAUSIBLE[field]
    return lo <= v <= hi


# ---------------------------------------------------------------- CO-OPS

def fetch_coops(get_json, st, begin, end):
    """(rows, errors) for one CO-OPS gauge. A product that answers 'no data' is a quiet hour, not an error."""
    rows, errors = [], []
    for product in st["products"]:
        q = {"product": product, "application": "CrackedBuckeye", "station": st["id"], "time_zone": "gmt",
             "units": "metric", "format": "json",
             "begin_date": begin.strftime("%Y%m%d %H:%M"), "end_date": end.strftime("%Y%m%d %H:%M")}
        if product == "water_level":
            q["datum"] = "IGLD"      # International Great Lakes Datum 1985: metres (feet) above sea level
        try:
            doc = get_json(COOPS_API + "?" + urllib.parse.urlencode(q))
        except Exception as e:
            errors.append(f"CO-OPS {st['id']} {product}: {e}")
            continue
        if "error" in doc:
            msg = (doc["error"] or {}).get("message", "")
            if "No data was found" not in msg:
                errors.append(f"CO-OPS {st['id']} {product}: {msg[:100]}")
            continue
        for d in doc.get("data", []):
            try:
                t = dt.datetime.strptime(d["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            except (KeyError, ValueError):
                continue
            suspect = "1" in (d.get("f") or "").split(",")   # CO-OPS' own limit / rate / flat-line flags
            flag = (d.get("q") or "") + (";suspect" if suspect else "")
            for key, field in COOPS_PRODUCTS[product]:
                try:
                    v = float(d[key])
                except (KeyError, TypeError, ValueError):
                    continue
                if _ok(field, v):
                    rows.append({"time_utc": _iso(t), "lake": SLUG, "source": "coops",
                                 "series": f"coops:{st['id']}:{field}", "value": v, "unit": FIELDS[field][1],
                                 "flag": flag.strip(";")})
    return rows, errors


# ---------------------------------------------------------------- NDBC

def ndbc_url(sid, hours):
    if hours <= 114:
        return f"{NDBC}/data/5day2/{sid.upper()}_5day.txt"
    return f"{NDBC}/data/realtime2/{sid.upper()}.txt"      # 45 days


def parse_ndbc(text, sid, since):
    """Rows from an NDBC standard-meteorological file (newest first, MM = missing). Only readings after `since`."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 3 or not lines[0].startswith("#"):
        return []
    cols = lines[0].lstrip("#").split()
    rows = []
    for l in lines[2:]:
        p = l.split()
        if len(p) < len(cols):
            continue
        try:
            t = dt.datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=UTC)
        except ValueError:
            continue
        if t < since:
            break      # newest first: everything after this is older
        for k, col in enumerate(cols):
            field = NDBC_COLUMNS.get(col)
            if not field or p[k] == "MM":
                continue
            try:
                v = float(p[k])
            except ValueError:
                continue
            if _ok(field, v):
                rows.append({"time_utc": _iso(t), "lake": SLUG, "source": "ndbc", "series": f"ndbc:{sid}:{field}",
                             "value": v, "unit": FIELDS[field][1], "flag": ""})
    return rows


def fetch_ndbc(get_text, st, hours, now):
    """(rows, errors) for one NDBC station. A 404 is a retired station; an empty file is a buoy out of the water."""
    try:
        text = get_text(ndbc_url(st["id"], hours))
    except Exception as e:
        return [], [f"NDBC {st['id']}: {e}"]
    return parse_ndbc(text, st["id"], now - dt.timedelta(hours=hours + 1)), []


# ---------------------------------------------------------------- NWS nearshore marine forecast

_PERIOD = re.compile(r"^\.(?!\.)([A-Z][A-Z0-9 /'-]*?)\.\.\.(.*)$")
_ISSUED = re.compile(r"^(\d{1,2})(\d{2}) ([AP]M) (E[DS]T) \w{3} (\w{3}) (\d{1,2}) (\d{4})$")
_MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def _issued_utc(line):
    m = _ISSUED.match(line.strip())
    if not m:
        return None
    h, mi, ap, tz, mon, day, yr = m.groups()
    hour = int(h) % 12 + (12 if ap == "PM" else 0)
    local = dt.datetime(int(yr), _MONTHS[mon], int(day), hour, int(mi))
    return _iso((local + dt.timedelta(hours=4 if tz == "EDT" else 5)).replace(tzinfo=UTC))


def _ugc_ids(line):
    """'LEZ142-143-102015-' -> LEZ142, LEZ143;  'LEZ144>146-102015-' -> LEZ144, LEZ145, LEZ146 (the last part is the expiry)."""
    parts = [p for p in line.strip().split("-") if p]
    if parts and re.fullmatch(r"\d{6}", parts[-1]):
        parts.pop()
    out, prefix = [], ""
    for p in parts:
        m = re.fullmatch(r"(?:([A-Z]{2}Z))?(\d{3})(?:>(\d{3}))?", p)
        if not m:
            continue
        prefix = m.group(1) or prefix
        for n in range(int(m.group(2)), int(m.group(3) or m.group(2)) + 1):
            out.append(f"{prefix}{n:03d}")
    return out


def parse_nsh(text, name):
    """One NWS Nearshore Marine Forecast file -> {ids, names, issued, issued_utc, expires_utc, headlines, periods, notes}."""
    lines = text.replace("\r", "").split("\n")
    expires = None
    m = re.match(r"Expires:(\d{12})", lines[0]) if lines else None
    if m:
        expires = _iso(dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=UTC))
    i = next((k for k, l in enumerate(lines) if re.match(r"^[A-Z]{2}Z\d{3}", l)), None)
    if i is None:
        raise ValueError("no UGC line in " + name)
    ids = _ugc_ids(lines[i])
    j = i + 1
    names = ""
    while j < len(lines) and not _ISSUED.match(lines[j].strip()):
        seg = lines[j].strip()
        names += seg if seg.endswith("-") else seg + " "     # a line may break inside "Geneva-on-the-Lake"
        j += 1
    zone_names = [n.strip() for n in re.split(r"(?<=\b(?:OH|NY|PA|MI))-", names) if n.strip()]
    issued = lines[j].strip() if j < len(lines) else ""
    headlines, periods, notes, cur = [], [], [], None
    in_notes, para = False, None      # after the last period and a blank line: free-text paragraphs
    for l in lines[j + 1:]:
        s = l.strip()
        if s.startswith("$$"):
            break
        if not s:
            if periods and (cur is not None or para is not None):
                in_notes = True
            cur, para = None, None
            continue
        if in_notes and not _PERIOD.match(s) and not s.startswith("..."):
            if para is None:
                notes.append(s)
                para = True
            else:
                notes[-1] += " " + s
            continue
        pm = _PERIOD.match(s)
        if s.startswith("..."):
            cur = {"text": s}
            headlines.append(cur)
            in_notes = False
        elif pm and re.search(r"(ADVISORY|WARNING|WATCH|STATEMENT)", pm.group(1)):
            cur = {"text": s}      # a hazard headline written with one dot instead of three
            headlines.append(cur)
            in_notes = False
        elif pm:
            cur = {"name": pm.group(1).strip().capitalize(), "text": pm.group(2).strip()}
            periods.append(cur)
            in_notes = False
        elif cur is not None:
            cur["text"] = (cur["text"] + " " + s).strip()
    return {"file": name, "ids": ids, "names": zone_names, "issued": issued, "issued_utc": _issued_utc(issued),
            "expires_utc": expires, "headlines": [h["text"].strip(".").strip() for h in headlines],
            "periods": [{"name": p["name"], "text": p["text"]} for p in periods], "notes": notes}


def fetch_marine(get_text, files):
    """({file: parsed forecast}, errors). The same product is filed under every zone it covers; one file per group."""
    out, errors = {}, []
    for f in files:
        try:
            out[f] = parse_nsh(get_text(NWS_TEXT + f + ".txt"), f)
        except Exception as e:
            errors.append(f"NWS marine {f}: {e}")
    return out, errors


# ---------------------------------------------------------------- the public files

def _station_key(st):
    return f"{st['src']}:{st['id']}"


def build(L, by, marine, marine_saved, last_seen, now, summarize):
    """(doc for lake/lakeerie.json, slim headline for now.json) from the registry entry, the recent readings
    by[(lake, series)] -> [(time, value)] in the agencies' units, and the parsed forecasts."""
    E = L["erie"]
    stations = []
    for st in E["coops"]:
        stations.append(dict(st, src="coops", kind="shore gauge", operator="NOAA CO-OPS",
                             url=f"https://tidesandcurrents.noaa.gov/stationhome.html?id={st['id']}"))
    for st in E["ndbc"]:
        stations.append(dict(st, src="ndbc", operator=st.get("owner") or "NOAA NDBC",
                             url=f"https://www.ndbc.noaa.gov/station_page.php?station={st['id']}"))
    stations.sort(key=lambda s: (s["lon"], s["id"]))      # west to east along the Ohio shore

    groups, recs, items_by = [], [], {}
    for st in stations:
        sk = _station_key(st)
        items = []
        for field in FIELD_ORDER:
            pts = by.get((SLUG, f"{sk}:{field}"))
            if not pts:
                continue
            label, _, unit, factor = FIELDS[field]
            pts = [(t, round(v * factor, 3)) for t, v in pts]
            item = {"key": f"{sk}:{field}", "field": field, "label": label, "unit": unit}
            item.update(summarize(pts))
            hourly = item.pop("hourly", None)
            if field in TRACE and _trace(hourly, TRACE[field]):
                item["trace"] = _trace(hourly, TRACE[field])
            items.append(item)
        items_by[sk] = {i["field"]: i for i in items}
        newest = max((i["time"] for i in items), default=None)
        if newest is None:     # nothing in the last 8 days: say when it last reported, from the archive's own state
            seen = [v for k, v in last_seen.items() if k.startswith(f"{SLUG}|{sk}:")]
            newest = max(seen) if seen else None
        age = (now - dt.datetime.fromisoformat(newest)) if newest else None
        vals = {i["field"]: i["value"] for i in items if not i["stale"]}
        # a gust is reported only with a wind; never show one from an older report than the wind beside it
        w, g = items_by[sk].get("wind_speed"), items_by[sk].get("wind_gust")
        if g and (not w or g["time"] != w["time"]):
            vals.pop("wind_gust", None)
        rec = {"id": st["id"], "source": st["src"], "name": st["name"], "kind": st.get("kind"), "operator": st["operator"],
               "lat": st["lat"], "lon": st["lon"], "url": st["url"], "reporting": bool(age is not None and age <= FRESH),
               "last_report": newest, "values": vals}
        if st["src"] == "coops" and "level" in vals and L["erie"].get("lwd_ft"):
            rec["above_chart_datum_ft"] = round(vals["level"] - L["erie"]["lwd_ft"], 2)
        recs.append(rec)
        if items:
            groups.append({"source": "NOAA CO-OPS" if st["src"] == "coops" else "NOAA NDBC", "id": st["id"],
                           "name": f"{st['name']} ({st['id']})" + ("" if st["src"] == "coops" else f", {st['operator']}"),
                           "role": st.get("kind"), "url": st["url"], "series": items})

    def fresh_item(sk, field):
        i = items_by.get(sk, {}).get(field)
        return i if i and (now - dt.datetime.fromisoformat(i["time"])) <= FRESH else None

    def first(order, field):
        for sk in order:
            i = fresh_item(sk, field)
            if i:
                return sk, i
        return None, None

    names = {_station_key(s): s["name"] for s in stations}
    shore = ["coops:9063063", "coops:9063079", "coops:9063053", "coops:9063085"]     # Cleveland first: the middle of the Ohio shore
    others = [k for k in names if k not in shore]
    head = {}
    sk, i = first(shore, "level")
    if i:
        head["level"] = dict(i, source="NOAA CO-OPS", station=names[sk], datum="IGLD 1985")
        if E.get("lwd_ft"):
            head["level"]["above_chart_datum_ft"] = round(i["value"] - E["lwd_ft"], 2)
            head["level"]["chart_datum_ft"] = E["lwd_ft"]
        gauges = [{"name": names[k], "value": fresh_item(k, "level")["value"]} for k in shore if fresh_item(k, "level")]
        if len(gauges) > 1:
            head["level"]["gauges"] = gauges
    sk, i = first(shore + others, "water_temp")
    if i:
        head["water_temp"] = dict(i, source="NOAA", station=names[sk], where="at " + names[sk])
        temps = [fresh_item(k, "water_temp") for k in names]
        vals = [t["value"] for t in temps if t]
        if len(vals) > 1:
            head["water_temp"]["range"] = {"low": min(vals), "high": max(vals), "n": len(vals)}
    sk, i = first(["ndbc:45005"] + shore + ["ndbc:45176", "ndbc:45165"], "wind_speed")
    if i:
        gust = items_by[sk].get("wind_gust")
        gust = gust if gust and gust["time"] == i["time"] else None
        d = items_by[sk].get("wind_dir")
        head["wind"] = {"station": sk.split(":")[1], "name": names[sk], "time": i["time"], "speed_kt": i["value"],
                        "gust_kt": (gust or {}).get("value"),
                        "dir_deg": d["value"] if d and d["time"] == i["time"] else None}
    sk, i = first(shore + others, "air_temp")
    if i:
        head["air_temp"] = {"value": i["value"], "unit": "degC", "time": i["time"], "station": names[sk]}
    sk, i = first(shore + others, "pressure")
    if i:
        head["pressure"] = {"value": i["value"], "unit": "hPa", "time": i["time"], "station": names[sk]}
    waves = [(names[k], fresh_item(k, "wave_height")) for k in names if fresh_item(k, "wave_height")]
    if waves:
        vals = [w["value"] for _, w in waves]
        top = max(waves, key=lambda x: x[1]["value"])
        head["waves"] = {"low_ft": min(vals), "high_ft": max(vals), "n": len(vals), "unit": "ft",
                         "highest_at": top[0], "time": max(w["time"] for _, w in waves)}

    forecast_files = []
    saved_next = {}
    for f in E["marine"]:
        cur = marine.get(f["file"])
        if cur:
            saved_next[f["file"]] = cur
        else:
            cur = (marine_saved or {}).get(f["file"])
            if cur:
                cur = dict(cur, stale=True)      # the fetch failed this hour: show the last one, marked
        if cur:
            if cur.get("expires_utc") and cur["expires_utc"] < now.isoformat():
                cur = dict(cur, expired=True)
            cur = dict(cur, url="https://forecast.weather.gov/shmrn.php?mz=" + (cur["ids"][0].lower() if cur.get("ids") else f["file"]))
            forecast_files.append(cur)
    mf = {"source": "National Weather Service Cleveland, Nearshore Marine Forecast (waters within five nautical "
                    "miles of shore)", "url": "https://www.weather.gov/cle/marine", "zones": forecast_files}
    ok = [s for s in recs if s["reporting"]]
    doc = {"slug": SLUG, "name": L["name"], "updated_utc": now.isoformat(), "operator": L.get("operator"),
           "kind": "great_lake", "headline": head, "stations": recs, "groups": groups, "marine": mf,
           "forecast": {}, "profile": None,
           "counts": {"stations": len(recs), "reporting": len(ok)},
           "note": "Provisional data, mirrored hourly from NOAA: water level, water temperature and wind at the "
                   "Center for Operational Oceanographic Products and Services' (CO-OPS) four Ohio gauges; buoys and "
                   "shore stations relayed by the National Data Buoy Center from NDBC, LimnoTech and the Cleveland "
                   "Water Alliance; and the National Weather Service's nearshore marine forecast. Water level is "
                   "feet above sea level (International Great Lakes Datum 1985). Buoys are taken out of the water "
                   "in the cold months, so some stations stop reporting. Readings may be revised by the agencies. "
                   "Not for navigation."}
    slim = {k: ({kk: vv for kk, vv in v.items() if kk not in ("hourly", "trace")} if isinstance(v, dict) else v)
            for k, v in head.items()}
    now_entry = {"name": L["name"], **slim, "stations_reporting": len(ok), "stations": len(recs)}
    return doc, now_entry, saved_next
