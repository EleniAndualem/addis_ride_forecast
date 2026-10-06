"""Freeze the forecast API into JSON for the Vercel deployment of the web app.

The 1–14 November inputs (zones, weather forecast, events) are fixed, so the API has a finite set of answers:
1 meta response, 14 city days and 168 zone-days. This script calls the real FastAPI app (app/app.py) for every
one of them and writes the responses to app/web/data/. On Vercel, Next.js route handlers serve these files on
the same /api/... paths, so the front end behaves identically with or without the Python server.

Run from the repo root after `python -m src.app_assets`:

    python app/export_static_api.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import app as api  # noqa: E402  (app/app.py)

OUT = HERE / "web" / "data"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    client = TestClient(api.app)

    def get(path: str) -> dict:
        r = client.get(path)
        r.raise_for_status()
        return r.json()

    meta = get("/api/meta")
    city = {d: get(f"/api/city?date={d}") for d in meta["dates"]}
    forecasts = {f"{z['zone']}|{d}": get(f"/api/forecast?zone={z['zone']}&date={d}")
                 for z in meta["zones"] for d in meta["dates"]}

    for name, payload in {"meta": meta, "city": city, "forecasts": forecasts}.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"{path.relative_to(HERE.parent)}  {path.stat().st_size / 1024:.0f} KB")
    print(f"{len(city)} days, {len(forecasts)} zone-days")


if __name__ == "__main__":
    main()
