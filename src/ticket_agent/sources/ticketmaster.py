"""Ticketmaster Discovery API v2 (free key from developer.ticketmaster.com, 5,000 calls/day).

Gives the event's face-value price range for primary sale (often missing) and its sale status.
Resale listings with sections/rows are partner-only, so this source is information, not alerts.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from . import EventInfo, SourceError

API = "https://app.ticketmaster.com/discovery/v2"


def parse_event(e: dict) -> EventInfo:
    start = (e.get("dates") or {}).get("start") or {}
    local = None
    if start.get("localDate"):
        local = start["localDate"] + "T" + (start.get("localTime") or "00:00:00")
    venues = (e.get("_embedded") or {}).get("venues") or [{}]
    ranges = [r for r in e.get("priceRanges") or [] if r.get("currency", "USD") == "USD"]
    mins = [r["min"] for r in ranges if r.get("min") is not None]
    maxs = [r["max"] for r in ranges if r.get("max") is not None]
    return EventInfo(
        source="ticketmaster", id=str(e["id"]), title=e.get("name") or "",
        starts_local=local, starts_utc=start.get("dateTime"),
        venue=venues[0].get("name") or "", city=(venues[0].get("city") or {}).get("name") or "",
        url=e.get("url") or "", lowest=min(mins) if mins else None,
        highest=max(maxs) if maxs else None,
        status=((e.get("dates") or {}).get("status") or {}).get("code"),
    )


def event_id_from_url(url: str) -> str | None:
    """ticketmaster.com/<slug>/event/3B00612F... -> '3B00612F...'."""
    host = urlparse(url).hostname or ""
    if "ticketmaster." not in host:
        return None
    parts = urlparse(url).path.rstrip("/").split("/")
    return parts[-1] if "event" in parts and parts[-1] != "event" else None


class Ticketmaster:
    name = "ticketmaster"
    label = "Ticketmaster"

    def __init__(self, api_key: str, timeout: float = 20, client: httpx.Client | None = None):
        self.api_key = api_key
        self.http = client or httpx.Client(timeout=timeout, headers={"User-Agent": "ticket-agent"})

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self.http.get(f"{API}{path}", params={**params, "apikey": self.api_key})
        except httpx.HTTPError as e:
            raise SourceError(f"Ticketmaster unreachable: {type(e).__name__}") from None
        if r.status_code in (401, 403):
            raise SourceError(f"Ticketmaster refused the API key (HTTP {r.status_code})")
        if r.status_code == 404:
            raise SourceError("Ticketmaster: event not found")
        if r.status_code >= 400:
            raise SourceError(f"Ticketmaster HTTP {r.status_code}")
        return r.json()

    def search(self, query: str, on: str | None = None, limit: int = 10) -> list[EventInfo]:
        params: dict = {"keyword": query, "size": limit, "sort": "date,asc", "countryCode": "US"}
        if on:
            # widen by a day each side: the filter is UTC, the event date is local
            d = date.fromisoformat(on)
            params["startDateTime"] = f"{(d - timedelta(days=1)).isoformat()}T00:00:00Z"
            params["endDateTime"] = f"{(d + timedelta(days=2)).isoformat()}T00:00:00Z"
        events = ((self._get("/events.json", params).get("_embedded") or {}).get("events")) or []
        out = [parse_event(e) for e in events]
        return [e for e in out if not on or e.date == on]

    def event(self, event_id: str) -> EventInfo:
        return parse_event(self._get(f"/events/{event_id}.json", {}))
