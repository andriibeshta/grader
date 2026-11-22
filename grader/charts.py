"""Chart generation from stored grades."""
from __future__ import annotations

import json
import statistics
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import db
from .config import DATA_DIR

CHART_PATH = DATA_DIR / "chart.png"
CHART_JSON = DATA_DIR / "chart.json"
CHART_LOCK = threading.Lock()


def _font(size: int, bold: bool = False):
    p = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    return ImageFont.truetype(p, size)


def _duration(row: dict) -> float | None:
    try:
        md = json.loads(row.get("metadata_json") or "{}")
        v = md.get("duration_seconds")
        return float(v) if v else None
    except Exception:
        return None


def render_chart() -> str:
    with CHART_LOCK:
        return _render_chart()


def _render_chart() -> str:
    rows = [r for r in db.scores(5000) if r["report_usable"] == 1]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = r.get("model_id") or r.get("source_type") or "unknown"
        groups.setdefault(key, []).append(r)

    W, H = 1500, 850
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 35), "Paper grader — quality summary", font=_font(30, True), fill=(20, 20, 20))
    d.text((60, 78), f"{len(rows)} usable grades · {len(groups)} model/source groups", font=_font(16), fill=(90, 90, 90))
    if not groups:
        d.text((60, 150), "No usable grades yet.", font=_font(20), fill=(120, 120, 120))
        img.save(CHART_PATH)
        return str(CHART_PATH)

    stats = []
    for name, rs in groups.items():
        q = [r["overall"] for r in rs]
        ds = [x for x in (_duration(r) for r in rs) if x is not None]
        stats.append({"name": name, "n": len(rs), "quality": round(statistics.mean(q), 2),
                      "time": round(statistics.mean(ds), 1) if ds else None})
    stats.sort(key=lambda x: x["quality"], reverse=True)
    timed = [s for s in stats if s["time"]]

    if len(timed) >= 2:
        L, R, T, B = 90, 900, 130, 720
        max_t = max(s["time"] for s in timed) * 1.15 or 1
        def X(t): return L + (t / max_t) * (R - L)
        def Y(q): return B - (q / 10) * (B - T)
        for q in range(0, 11, 2):
            y = Y(q); d.line([(L, y), (R, y)], fill=(228, 231, 235), width=1); d.text((L-35, y-8), str(q), font=_font(14), fill=(80,80,80))
        for i in range(0, 6):
            t = max_t * i / 5; x = X(t); d.line([(x, T), (x, B)], fill=(240, 242, 245), width=1); d.text((x-20, B+12), f"{t:.0f}", font=_font(14), fill=(80,80,80))
        d.line([(L,T),(L,B)], fill=(60,60,60), width=2); d.line([(L,B),(R,B)], fill=(60,60,60), width=2)
        d.text((L+(R-L)//2-80, B+45), "Mean duration (s)", font=_font(16), fill=(60,60,60))
        d.text((20, T+220), "Mean quality", font=_font(16), fill=(60,60,60))
        colors = [(31,119,180),(214,39,40),(44,160,44),(255,127,14),(148,103,189),(140,86,75),(23,190,207),(227,119,194)]
        for i, s in enumerate(timed):
            x, y = X(s["time"]), Y(s["quality"]); c = colors[i % len(colors)]
            d.ellipse([x-9,y-9,x+9,y+9], fill=c, outline="white", width=2)
            d.text((x+12, y-10), f"{s['name']}  {s['quality']:.2f}/{s['time']:.0f}s", font=_font(14), fill=(40,40,40))
    else:
        # Bar chart when durations are unavailable.
        max_q = 10
        y0 = 180; bar_h = 55
        for i, s in enumerate(stats):
            y = y0 + i * (bar_h + 28)
            d.text((60, y+8), s["name"][:34], font=_font(16), fill=(40,40,40))
            w = int((s["quality"] / max_q) * 520)
            d.rectangle([420, y, 420+w, y+bar_h], fill=(31,119,180))
            d.text((430+w, y+12), f"{s['quality']:.2f} (n={s['n']})", font=_font(15), fill=(40,40,40))

    # Right-side table
    x0 = 1030; y = 150
    d.text((x0, 115), "Group", font=_font(16, True), fill=(30,30,30))
    d.text((x0+250, 115), "n", font=_font(16, True), fill=(30,30,30))
    d.text((x0+295, 115), "quality", font=_font(16, True), fill=(30,30,30))
    for s in stats[:10]:
        label = s["name"] if len(s["name"]) <= 26 else s["name"][:25] + "…"
        d.text((x0, y), label, font=_font(14), fill=(50,50,50))
        d.text((x0+250, y), str(s["n"]), font=_font(14), fill=(50,50,50))
        d.text((x0+295, y), f"{s['quality']:.2f}", font=_font(14), fill=(50,50,50))
        y += 28
    d.text((60, 800), "Failures are stored with report_usable=0 and excluded from the quality chart.", font=_font(14), fill=(110,110,110))
    img.save(CHART_PATH)
    CHART_JSON.write_text(json.dumps(stats, indent=1))
    return str(CHART_PATH)
