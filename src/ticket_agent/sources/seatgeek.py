"""SeatGeek Platform API (free client id from seatgeek.com/account/develop).

GET https://api.seatgeek.com/2/events?q=...&client_id=...  -> events with a `stats` block:
listing_count, lowest_price, average_price, highest_price. No seat-level data.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from urllib.parse import urlencode, urlparse

import httpx

from . import EventInfo, SourceError

log = logging.getLogger(__name__)
API = "https://api.seatgeek.com/2"


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_event(e: dict) -> EventInfo:
    stats = e.get("stats") or {}
    venue = e.get("venue") or {}
    count = stats.get("listing_count")
    if count is None:
        count = stats.get("visible_listing_count")
    utc = e.get("datetime_utc")
    return EventInfo(
        source="seatgeek", id=str(e["id"]), title=e.get("title") or e.get("short_title") or "",
        starts_local=e.get("datetime_local"), starts_utc=(utc + "Z") if utc and not utc.endswith("Z") else utc,
        venue=venue.get("name") or "", city=venue.get("city") or "", url=e.get("url") or "",
        lowest=_num(stats.get("lowest_price")), average=_num(stats.get("average_price")),
        highest=_num(stats.get("highest_price")), listings=int(count) if count is not None else None,
    )


def event_id_from_url(url: str) -> str | None:
    """seatgeek.com/knicks-at-...-tickets/nba/2026-11-21-7-30-pm/17654321 -> '17654321'."""
    host = urlparse(url).hostname or ""
    if not (host == "seatgeek.com" or host.endswith(".seatgeek.com")):
        return None
    m = re.search(r"/(\d{6,})/?(?:\?|$)", url)
    return m.group(1) if m else None


def buy_url(url: str, quantity: int) -> str:
    """The event page with the ticket quantity preselected."""
    if not url:
        return url
    return url + ("&" if "?" in url else "?") + urlencode({"quantity": quantity})


class SeatGeek:
    name = "seatgeek"
    label = "SeatGeek"

    def __init__(self, client_id: str, timeout: float = 20, client: httpx.Client | None = None):
        self.client_id = client_id
        self.http = client or httpx.Client(timeout=timeout, headers={"User-Agent": "ticket-agent"})

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self.http.get(f"{API}{path}", params={**params, "client_id": self.client_id})
        except httpx.HTTPError as e:
            raise SourceError(f"SeatGeek unreachable: {type(e).__name__}") from None
        if r.status_code in (401, 403):
            raise SourceError(f"SeatGeek refused the client id (HTTP {r.status_code})")
        if r.status_code == 404:
            raise SourceError("SeatGeek: event not found")
        if r.status_code >= 400:
            raise SourceError(f"SeatGeek HTTP {r.status_code}")
        return r.json()

    def search(self, query: str, on: str | None = None, limit: int = 10) -> list[EventInfo]:
        """Events matching `query`; `on` (YYYY-MM-DD) limits to that local date."""
        params: dict = {"q": query, "per_page": limit, "sort": "datetime_local.asc"}
        if on:
            d = date.fromisoformat(on)
            params["datetime_local.gte"] = d.isoformat()
            params["datetime_local.lt"] = (d + timedelta(days=1)).isoformat()
        return [parse_event(e) for e in self._get("/events", params).get("events") or []]

    def event(self, event_id: str) -> EventInfo:
        return parse_event(self._get(f"/events/{event_id}", {}))
