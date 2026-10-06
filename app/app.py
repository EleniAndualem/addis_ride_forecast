"""Deliverable E — Addis ride-demand forecast demo (API + web app).

One command from the repo root starts everything:

    python app/app.py            # then open http://localhost:8000

This FastAPI server loads the final model and the lookup tables bundled in app/assets/ (built by
`python -m src.app_assets` from the cleaned data), answers the forecast API, and serves the pre-built
Next.js front end from app/web/out/. The user only ever chooses a zone and a date; weather and events are
looked up here. Nothing is cleaned, joined or fitted at request time.
"""
from __future__ import annotations

import base64
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
ASSETS = HERE / "assets"
WEB_OUT = HERE / "web" / "out"
FIRST_DAY, LAST_DAY = pd.Timestamp("2025-11-01"), pd.Timestamp("2025-11-14")
TRIPS_PER_DRIVER_HOUR = 1.3

# Approximate neighbourhood centres, for the map only (not used by the model).
ZONE_COORDS = {
    "Arat Kilo": (9.0335, 38.7630), "Ayat": (9.0205, 38.8790), "Bole": (8.9955, 38.7895),
    "CMC": (9.0225, 38.8450), "Gerji": (8.9985, 38.8155), "Kazanchis": (9.0150, 38.7665),
    "Kolfe": (9.0290, 38.7010), "Lideta": (9.0105, 38.7395), "Megenagna": (9.0200, 38.8015),
    "Merkato": (9.0330, 38.7350), "Piassa": (9.0375, 38.7515), "Sarbet": (8.9965, 38.7440),
}
ZONE_TYPE_LABELS = {"business_district": "Business district", "residential": "Residential",
                    "transport_hub": "Transport hub", "market": "Market", "nightlife_airport": "Nightlife & airport"}
EVENT_LABELS = {"football_match": "Football match", "concert": "Concert", "conference": "Conference",
                "exhibition": "Exhibition", "road_closure": "Road closure", "sports_run": "Road race",
                "public_holiday": "Public holiday", "school_break": "School break"}
EVENT_ICONS = {"football_match": "⚽", "concert": "🎵", "conference": "🎤", "exhibition": "🖼️",
               "road_closure": "🚧", "sports_run": "🏃", "public_holiday": "🎉", "school_break": "🎒"}

# ----------------------------------------------------------------------------------------------------
# Basemap (CARTO). Tiles are fetched by this server, so the browser only ever talks to localhost and an
# API key, if you have one, never reaches the front end or the repository. Put it in app/.env:
#     CARTO_API_KEY=your-key
# (app/.env is git-ignored). CARTO's public basemaps also work without a key.
# ----------------------------------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(HERE / ".env")
CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "").strip()
CARTO_STYLES = {"dark": "dark_all", "light": "light_all", "voyager": "rastertiles/voyager"}
CARTO_ATTRIBUTION = "© OpenStreetMap contributors © CARTO"
# 1x1 transparent PNG, returned when a tile cannot be fetched (offline demo) so the map never errors.
EMPTY_TILE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")
_TILE_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_TILE_LOCK = threading.Lock()
TILE_CACHE_SIZE = 3000

# ----------------------------------------------------------------------------------------------------
# Load once
# ----------------------------------------------------------------------------------------------------
BUNDLE = joblib.load(ASSETS / "final_model.joblib")
MODEL, FEATURES = BUNDLE["model"], BUNDLE["features"]
TEST = pd.read_csv(ASSETS / "master_test.csv", parse_dates=["pickup_hour"])
WEATHER = pd.read_csv(ASSETS / "weather_forecast.csv", parse_dates=["pickup_hour"])
EVENTS = pd.read_csv(ASSETS / "events.csv", parse_dates=["start", "end"])
PROFILE = pd.read_csv(ASSETS / "typical_profile.csv")
FARES = pd.read_csv(ASSETS / "zone_avg_fare.csv").set_index("zone")["avg_fare_birr"]
QUANT = pd.read_csv(ASSETS / "interval_quantiles.csv").set_index("level")
ZONES = sorted(TEST["zone"].unique())
ZONE_TYPE = TEST.drop_duplicates("zone").set_index("zone")["zone_type"]
EVENT_WINDOW_COLS = [c for c in TEST.columns if c.startswith("ev_") and c.endswith("_window")]

# Score the whole fortnight once at start-up (4,032 rows): every request is then a lookup.
TEST["forecast"] = np.clip(MODEL.predict(TEST[FEATURES]), 0, None)
_scale = np.sqrt(TEST["forecast"] + 1)
TEST["lower_80"] = np.clip(TEST["forecast"] + QUANT.loc[80, "q_low"] * _scale, 0, None)
TEST["upper_80"] = TEST["forecast"] + QUANT.loc[80, "q_high"] * _scale
TEST = TEST.merge(PROFILE, on=["zone", "dow", "hour"], how="left").rename(columns={"typical_trips": "typical"})
TEST["date"] = TEST["pickup_hour"].dt.normalize()


def parse_day(date: str) -> pd.Timestamp:
    try:
        day = pd.Timestamp(date).normalize()
    except (ValueError, TypeError):
        raise HTTPException(422, f"“{date}” is not a date we can read. Use YYYY-MM-DD, e.g. 2025-11-05.")
    if not FIRST_DAY <= day <= LAST_DAY:
        raise HTTPException(422, f"We only forecast 1–14 November 2025, so {day:%d %B %Y} isn't available. "
                                 "Please pick a day in that fortnight.")
    return day


def parse_zone(zone: str) -> str:
    match = {z.lower(): z for z in ZONES}.get(zone.strip().lower())
    if match is None:
        raise HTTPException(422, f"“{zone}” isn't one of our 12 zones.")
    return match


def zone_events(zone: str, day: pd.Timestamp) -> pd.DataFrame:
    day_end = day + pd.Timedelta(hours=23, minutes=59)
    in_zone = EVENTS["zones"].fillna("").str.split(";").apply(lambda zs: zone in zs or "Citywide" in zs)
    return EVENTS[in_zone & (EVENTS["end"] >= day) & (EVENTS["start"] <= day_end)].sort_values("start")


# ----------------------------------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------------------------------
app = FastAPI(title="Addis Ride Demand Forecast", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
                   allow_methods=["GET"], allow_headers=["*"], expose_headers=["X-Basemap"])


@app.get("/api/health")
def health():
    """Liveness check used by the front end and for a quick smoke test."""
    return {"status": "ok", "model": "LightGBM (Poisson)", "rows_scored": int(len(TEST)), "zones": len(ZONES),
            "first_day": f"{FIRST_DAY:%Y-%m-%d}", "last_day": f"{LAST_DAY:%Y-%m-%d}",
            "basemap": {"provider": "CARTO", "api_key_configured": bool(CARTO_API_KEY)}}


# Some Python installs (notably python.org builds on macOS) ship without CA certificates, so HTTPS calls to
# CARTO fail and the map goes blank. Use certifi's bundle when it is installed.
try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()


@app.get("/api/basemap/{style}/{z}/{x}/{y}.png")
def basemap_tile(style: str, z: int, x: int, y: int):
    """CARTO raster tile, fetched and cached by the server. Falls back to a transparent tile offline."""
    if style not in CARTO_STYLES:
        raise HTTPException(404, f"Unknown basemap style '{style}'. Use one of: {', '.join(CARTO_STYLES)}.")
    if not (0 <= z <= 20 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "Tile out of range.")
    key = (style, z, x, y)
    with _TILE_LOCK:
        if key in _TILE_CACHE:
            _TILE_CACHE.move_to_end(key)
            return Response(_TILE_CACHE[key], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    url = f"https://{'abcd'[(x + y) % 4]}.basemaps.cartocdn.com/{CARTO_STYLES[style]}/{z}/{x}/{y}@2x.png"
    # Try with the key first; if CARTO rejects the key, the public basemap still serves the tile without it.
    urls = [f"{url}?api_key={urllib.parse.quote(CARTO_API_KEY)}", url] if CARTO_API_KEY else [url]
    data = None
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "addis-ride-demand/1.0"})
            with urllib.request.urlopen(req, timeout=8, context=_SSL) as r:
                data = r.read()
            break
        except urllib.error.HTTPError:
            continue
        except (urllib.error.URLError, TimeoutError, OSError):
            break
    if data is None:
        return Response(EMPTY_TILE, media_type="image/png", headers={"Cache-Control": "no-store", "X-Basemap": "offline"})
    with _TILE_LOCK:
        _TILE_CACHE[key] = data
        while len(_TILE_CACHE) > TILE_CACHE_SIZE:
            _TILE_CACHE.popitem(last=False)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/meta")
def meta():
    v = BUNDLE["validation"]
    return {
        "team": BUNDLE["team"],
        "model": {"name": "LightGBM (Poisson)", "n_features": len(FEATURES), "trained_on": BUNDLE["trained_on"],
                  "rmse": round(float(v["main_split_rmse"]), 2), "mae": round(float(v["main_split_mae"]), 2),
                  "rolling_rmse": round(float(v["rolling_rmse_mean"]), 2),
                  "rolling_rmse_sd": round(float(v["rolling_rmse_sd"]), 2), "n_train": int(BUNDLE["n_train"]),
                  "baseline_rmse": round(float(v["seasonal_naive_rmse"]), 2)},
        "trips_per_driver_hour": TRIPS_PER_DRIVER_HOUR,
        "basemap": {"tiles": "/api/basemap/{style}/{z}/{x}/{y}.png", "styles": list(CARTO_STYLES),
                    "default": "dark", "attribution": CARTO_ATTRIBUTION},
        "first_day": f"{FIRST_DAY:%Y-%m-%d}", "last_day": f"{LAST_DAY:%Y-%m-%d}",
        "dates": [f"{d:%Y-%m-%d}" for d in pd.date_range(FIRST_DAY, LAST_DAY)],
        "zones": [{"zone": z, "type": ZONE_TYPE[z], "type_label": ZONE_TYPE_LABELS.get(ZONE_TYPE[z], ZONE_TYPE[z]),
                   "lat": ZONE_COORDS[z][0], "lon": ZONE_COORDS[z][1], "fare": round(float(FARES[z]), 1)}
                  for z in ZONES],
    }


@app.get("/api/city")
def city(date: str = Query(..., description="YYYY-MM-DD within 1–14 Nov 2025")):
    """All 12 zones for one day: the hourly curve (for the map and the leaderboard)."""
    day = parse_day(date)
    d = TEST[TEST["date"] == day].sort_values("pickup_hour")
    out = []
    for z, g in d.groupby("zone"):
        hourly = g["forecast"].round(1).tolist()
        peak_i = int(np.argmax(hourly))
        has_event = bool(not zone_events(z, day).empty)
        out.append({"zone": z, "hourly": hourly, "total": round(float(g["forecast"].sum()), 0),
                    "usual_total": round(float(g["typical"].sum()), 0), "peak_hour": peak_i,
                    "peak": hourly[peak_i], "has_event": has_event,
                    "rain_mm": round(float(g["rain_mm"].sum()), 1)})
    return {"date": f"{day:%Y-%m-%d}", "weekday": f"{day:%A}", "zones": sorted(out, key=lambda r: -r["total"])}


@app.get("/api/forecast")
def forecast(zone: str = Query(...), date: str = Query(...)):
    """The 24-hour forecast for one zone and day, with everything that was looked up."""
    zone, day = parse_zone(zone), parse_day(date)
    d = TEST[(TEST["zone"] == zone) & (TEST["date"] == day)].sort_values("pickup_hour")
    if d.empty:
        raise HTTPException(404, f"No forecast is available for {zone} on {day:%d %B %Y}.")
    evs = zone_events(zone, day)
    fare = float(FARES[zone])
    hours = [{
        "hour": int(r.hour), "time": f"{int(r.hour):02d}:00",
        "forecast": round(float(r.forecast), 1), "lower_80": round(float(r.lower_80), 1),
        "upper_80": round(float(r.upper_80), 1), "typical": round(float(r.typical), 1),
        "drivers": int(np.ceil(r.forecast / TRIPS_PER_DRIVER_HOUR)),
        "drivers_safe": int(np.ceil(r.upper_80 / TRIPS_PER_DRIVER_HOUR)),
        "fares": round(float(r.forecast) * fare, 0), "temp_c": round(float(r.temp_c), 1),
        "rain_mm": round(float(r.rain_mm), 1), "event": bool(sum(getattr(r, c) for c in EVENT_WINDOW_COLS) > 0),
    } for r in d.itertuples()]

    w = WEATHER[WEATHER["pickup_hour"].dt.normalize() == day]
    chips, parts = [], []
    if not w.empty:
        t = f"{w.temp_c.min():.0f}–{w.temp_c.max():.0f} °C"
        chips.append({"kind": "weather", "icon": "🌡️", "text": t}); parts.append(t)
        rain = float(w.rain_mm.sum())
        if rain < 0.1:
            chips.append({"kind": "weather", "icon": "☀️", "text": "No rain forecast"}); parts.append("no rain")
        else:
            wet = w.loc[w.rain_mm.idxmax()]
            t = f"{rain:.1f} mm rain · heaviest {wet.rain_mm:.1f} mm at {wet.pickup_hour:%H:%M}"
            chips.append({"kind": "rain", "icon": "🌧️", "text": t}); parts.append(f"rain {wet.rain_mm:.1f} mm at {wet.pickup_hour:%H:%M}")
    windows = []
    for e in evs.itertuples():
        label = EVENT_LABELS.get(e.event_type, str(e.event_type).replace("_", " "))
        venue = e.venue if isinstance(e.venue, str) and e.venue not in ("(none)", "") else None
        t = f"{label}{' at ' + venue if venue else ''} · {e.start:%H:%M}–{e.end:%H:%M}"
        chips.append({"kind": "event", "icon": EVENT_ICONS.get(e.event_type, "📍"), "text": t})
        parts.append(f"{label.lower()}{' at ' + venue if venue else ''} {e.start:%H:%M}–{e.end:%H:%M}")
        s_h = 0 if e.start < day else int(e.start.hour)
        e_h = 23 if e.end >= day + pd.Timedelta(days=1) else int(e.end.hour)
        windows.append({"label": label, "icon": EVENT_ICONS.get(e.event_type, "📍"), "start_hour": s_h,
                        "end_hour": e_h, "venue": venue})
    if evs.empty:
        chips.append({"kind": "none", "icon": "📅", "text": "No listed events in or near this zone"})

    total, usual = float(d["forecast"].sum()), float(d["typical"].sum())
    peak = max(hours, key=lambda h: h["forecast"])
    return {
        "zone": zone, "zone_type": ZONE_TYPE_LABELS.get(ZONE_TYPE[zone], ZONE_TYPE[zone]),
        "date": f"{day:%Y-%m-%d}", "weekday": f"{day:%A}", "fare": round(fare, 1),
        "hours": hours, "events": windows,
        "lookup": {"chips": chips, "summary": "; ".join(parts)},
        "kpis": {"total": round(total), "usual_total": round(usual),
                 "vs_usual_pct": round(100 * (total / usual - 1), 1) if usual else 0.0,
                 "peak_hour": peak["time"], "peak_trips": peak["forecast"],
                 "peak_range": [peak["lower_80"], peak["upper_80"]],
                 "peak_drivers": peak["drivers"], "driver_hours": int(sum(h["drivers"] for h in hours)),
                 "gross_fares": round(total * fare)},
    }


# ----------------------------------------------------------------------------------------------------
# Front end (pre-built static Next.js export)
# ----------------------------------------------------------------------------------------------------
if WEB_OUT.exists():
    app.mount("/", StaticFiles(directory=WEB_OUT, html=True), name="web")
else:
    @app.get("/")
    def missing_frontend():
        return {"message": "Front end not built. Run `npm install && npm run build` in app/web, "
                           "or use the API at /api/health, /api/meta, /api/city, /api/forecast."}


if __name__ == "__main__":
    import uvicorn
    print("\n  Addis Ride Demand Forecast  →  http://localhost:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
