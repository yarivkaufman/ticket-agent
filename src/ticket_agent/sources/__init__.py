"""Official ticket APIs. Each source finds events and reports their current prices.

No scraping: ticket sites guard seat listings with bot protection, and this agent never tries
to get around it. Only free official APIs are used:
- SeatGeek (api.seatgeek.com): lowest / average / highest resale price and listing count.
- Ticketmaster Discovery API: the event's face-value price range (primary sale), shown for info.
"""

from __future__ import annotations

from dataclasses import dataclass


class SourceError(Exception):
    """The API failed or refused the request (bad key, rate limit, outage)."""


@dataclass
class EventInfo:
    source: str  # "seatgeek" | "ticketmaster"
    id: str
    title: str
    starts_local: str | None  # "2026-11-21T19:30:00" (venue local time)
    starts_utc: str | None  # ISO UTC
    venue: str
    city: str
    url: str
    lowest: float | None = None  # cheapest ticket now (SeatGeek: all-in per ticket)
    average: float | None = None
    highest: float | None = None
    listings: int | None = None
    status: str | None = None  # Ticketmaster: onsale / offsale / cancelled / postponed ...

    @property
    def date(self) -> str | None:
        return self.starts_local[:10] if self.starts_local else None
