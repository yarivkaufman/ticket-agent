"""Price-history chart: inline SVG step line of the lowest price, plus the target (no JS library)."""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

W, H = 720, 240
PAD_L, PAD_R, PAD_T, PAD_B = 56, 104, 12, 28


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = max(hi - lo, 1.0)
    raw = span / n
    mag = 10 ** len(str(int(raw))) / 10
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    t, ticks = int(lo // step) * step, []
    while t <= hi + step / 2:
        if t >= lo - step / 2:
            ticks.append(round(t, 2))
        t += step
    return ticks


def history_svg(rows, target: float | None, tz: str = "America/New_York", fee: float = 1.0) -> str:
    """rows: (at, lowest) ordered by time; lowest is SeatGeek's raw price (fee applied here)."""
    zone = ZoneInfo(tz)
    pts = [(datetime.fromisoformat(r["at"]), round(r["lowest"] * fee, 2) if r["lowest"] is not None else None)
           for r in rows]
    values = [v for _, v in pts if v is not None] + ([target] if target else [])
    times = [t for t, _ in pts]
    if len(set(times)) < 2 or not [v for _, v in pts if v is not None]:
        return ""
    t0, t1 = min(times), max(times)
    ticks = _nice_ticks(min(values), max(values))
    lo, hi = min(ticks[0], min(values)), max(ticks[-1], max(values))
    hi = hi if hi > lo else lo + 1
    pw, ph = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def x(t: datetime) -> float:
        return PAD_L + (t - t0).total_seconds() / max((t1 - t0).total_seconds(), 1) * pw

    def y(v: float) -> float:
        return PAD_T + (hi - v) / (hi - lo) * ph

    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Lowest ticket price over time">']
    for v in ticks:
        out.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text class="tick" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">${v:,.0f}</text>')
    for t, anchor in ((t0, "start"), (t1, "end")):
        out.append(f'<text class="tick" x="{x(t):.1f}" y="{H - 8}" text-anchor="{anchor}">'
                   f'{t.astimezone(zone).strftime("%b %d")}</text>')
    if target:
        out.append(f'<line class="target" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(target):.1f}" '
                   f'y2="{y(target):.1f}"/><text class="endlabel muted" x="{W - PAD_R + 8}" '
                   f'y="{y(target) + 4:.1f}">target ${target:,.0f}</text>')
    segs, cur = [], []
    for t, v in pts:  # step line: price holds until the next check; gaps where none listed
        if v is None:
            if cur:
                segs.append(cur)
            cur = []
            continue
        if cur:
            cur.append(f"{x(t):.1f},{cur[-1].split(',')[1]}")
        cur.append(f"{x(t):.1f},{y(v):.1f}")
    if cur:
        segs.append(cur)
    for seg in segs:
        if len(seg) == 1:
            cx, cy = seg[0].split(",")
            out.append(f'<circle class="dot" cx="{cx}" cy="{cy}" r="4"/>')
        else:
            out.append(f'<polyline class="line" points="{" ".join(seg)}"/>')
    last = next((v for _, v in reversed(pts) if v is not None), None)
    if last is not None:
        ly = y(last)
        if target and abs(ly - y(target)) < 14:
            ly += 14 if ly >= y(target) else -14
        out.append(f'<text class="endlabel" x="{W - PAD_R + 8}" y="{ly + 4:.1f}">'
                   f'{html.escape(f"now ${last:,.0f}")}</text>')
    out.append("</svg>")
    return "".join(out)
