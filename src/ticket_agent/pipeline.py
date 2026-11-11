"""Find the event for a request, check its prices, decide what to alert on."""

from __future__ import annotations

import html
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Config
from .db import DB, event_time, now
from .parser import EventRequest, Parser, best_match
from .sources import EventInfo, SourceError
from .sources import seatgeek as sg
from .sources import ticketmaster as tm

log = logging.getLogger(__name__)


def money(x: float | None) -> str:
    return "—" if x is None else f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"


def is_drop(old: float, new: float, cfg: Config) -> bool:
    drop = old - new
    return drop > 0 and (drop >= cfg.alerts.min_drop_usd or drop / old * 100 >= cfg.alerts.min_drop_pct)


def more_listings(old: int, new: int, cfg: Config) -> bool:
    grew = new - old
    return grew >= cfg.alerts.min_new_listings and (
        old == 0 or grew / old * 100 >= cfg.alerts.min_new_listings_pct)


@dataclass
class Decision:
    kinds: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    low: float | None = None
    listings: int | None = None
    under_target: bool = False


def decide(cfg: Config, state: sqlite3.Row | None, price: float | None, listings: int | None,
           target: float | None) -> Decision:
    """Compare a check with what was announced before. The first check only sets the baseline
    (the add reply already shows the price), except that being under target always alerts."""
    d = Decision(low=state["low_announced"] if state else None,
                 listings=state["listings_announced"] if state else None,
                 under_target=bool(state["under_target"]) if state else False)
    first = state is None
    if price is not None:
        under = target is not None and price <= target
        if under and not d.under_target:
            d.kinds.append("target")
            d.lines.append(f"✅ At or under your target: <b>{money(price)}</b> per ticket "
                           f"(target {money(target)})")
            d.low = price
        elif not first and d.low is not None and is_drop(d.low, price, cfg):
            d.kinds.append("low")
            d.lines.append(f"📉 New low: <b>{money(price)}</b> per ticket (was {money(d.low)})")
            d.low = price
        elif d.low is None or (first and not under):
            d.low = price
        d.under_target = under
    if listings is not None:
        if not first and d.listings is not None and more_listings(d.listings, listings, cfg):
            d.kinds.append("listings")
            d.lines.append(f"🆕 {listings - d.listings} more listings (now {listings})")
            d.listings = listings
        elif d.listings is None or listings < d.listings:
            d.listings = listings  # follow decreases so a rebound counts as new seats
    return d


@dataclass
class CheckResult:
    price: float | None = None
    listings: int | None = None
    alert_id: int | None = None
    errors: list[str] = field(default_factory=list)


class Tracker:
    def __init__(self, cfg: Config, db: DB, parser: Parser, seatgeek: sg.SeatGeek | None,
                 ticketmaster: tm.Ticketmaster | None):
        self.cfg, self.db, self.parser = cfg, db, parser
        self.seatgeek = seatgeek if cfg.source_on("seatgeek") else None
        self.ticketmaster = ticketmaster if cfg.source_on("ticketmaster") else None

    # --- adding -------------------------------------------------------------------------------

    def resolve(self, text: str) -> tuple[EventRequest, EventInfo | None, EventInfo | None]:
        """(request, SeatGeek event, Ticketmaster event) for what was typed or linked."""
        urls = re.findall(r"https?://[^\s<>\"']+", text)
        sg_id = next((i for u in urls if (i := sg.event_id_from_url(u))), None)
        tm_id = next((i for u in urls if (i := tm.event_id_from_url(u))), None)
        sg_ev = tm_ev = None
        if sg_id and self.seatgeek:
            sg_ev = self.seatgeek.event(sg_id)
        if tm_id and self.ticketmaster:
            tm_ev = self.ticketmaster.event(tm_id)
        linked = sg_ev or tm_ev
        req = self.parser.parse(f"{linked.title} {linked.date or ''} {linked.venue}\n{text}"
                                if linked else text)
        if linked:
            req.date = linked.date
        if self.seatgeek and sg_ev is None:
            sg_ev = self._find(self.seatgeek, req)
        if self.ticketmaster and tm_ev is None:
            try:
                tm_ev = self._find(self.ticketmaster, req)
            except SourceError as e:  # Ticketmaster is extra info; SeatGeek is what's tracked
                log.warning("ticketmaster search failed: %s", e)
        return req, sg_ev, tm_ev

    @staticmethod
    def _find(source, req: EventRequest) -> EventInfo | None:
        """Search with the full query, then (SeatGeek doesn't match "Knicks Trail Blazers") with
        each team/performer on its own, and pick the best match on the requested date."""
        for q in search_queries(req.search_query):
            ev = best_match(req, source.search(q, req.date))
            if ev:
                return ev
        return None

    def add(self, text: str) -> int:
        if not (self.seatgeek or self.ticketmaster):
            raise RuntimeError("no ticket source is set up (SEATGEEK_CLIENT_ID / TICKETMASTER_API_KEY)")
        req, sg_ev, tm_ev = self.resolve(text)
        main = sg_ev or tm_ev
        if main is None:
            when = f" on {req.date}" if req.date else ""
            raise LookupError(f"no upcoming event found for “{req.search_query}”{when}")
        return self.db.add_event(
            name=main.title, request=text, starts_local=main.starts_local, starts_utc=main.starts_utc,
            venue=", ".join(x for x in (main.venue, main.city) if x),
            quantity=req.quantity or self.cfg.default_quantity, max_price=req.max_price_per_ticket,
            seatgeek_id=sg_ev.id if sg_ev else None, seatgeek_url=sg_ev.url if sg_ev else None,
            tm_id=tm_ev.id if tm_ev else None, tm_url=tm_ev.url if tm_ev else None)

    # --- checking -----------------------------------------------------------------------------

    def interval(self, ev: sqlite3.Row, at: datetime) -> timedelta:
        s = self.cfg.schedule
        start = parse_utc(ev["starts_utc"])
        if start and start - at <= timedelta(hours=s.close_hours):
            return timedelta(minutes=s.close_every_minutes)
        return timedelta(minutes=s.check_every_minutes)

    def due(self, ev: sqlite3.Row, at: datetime) -> bool:
        last = parse_utc(ev["last_scan_at"])
        # 3 min slack so a check at :02 isn't skipped because the last one finished at :03
        return last is None or at - last >= self.interval(ev, at) - timedelta(minutes=3)

    def check(self, ev: sqlite3.Row) -> CheckResult:
        res = CheckResult()
        at = now()
        sg_now = tm_now = None
        if self.seatgeek and ev["seatgeek_id"]:
            try:
                sg_now = self.seatgeek.event(ev["seatgeek_id"])
                self.db.add_snapshot(ev["id"], at, "seatgeek", sg_now.lowest, sg_now.average,
                                     sg_now.highest, sg_now.listings)
            except SourceError as e:
                res.errors.append(f"seatgeek: {e}")
        if self.ticketmaster and ev["tm_id"]:
            try:
                tm_now = self.ticketmaster.event(ev["tm_id"])
                self.db.add_snapshot(ev["id"], at, "ticketmaster", tm_now.lowest, None,
                                     tm_now.highest, None, tm_now.status)
            except SourceError as e:
                res.errors.append(f"ticketmaster: {e}")
        if sg_now is not None and sg_now.lowest is None and sg_now.listings is None:
            res.errors.append("seatgeek: the API returned no price data for this event")
        self.db.update_event(ev["id"], last_scan_at=at, last_error="; ".join(res.errors) or None)
        if sg_now is None:
            return res
        price = round(sg_now.lowest * self.cfg.fee_multiplier, 2) if sg_now.lowest is not None else None
        res.price, res.listings = price, sg_now.listings
        d = decide(self.cfg, self.db.state(ev["id"]), price, sg_now.listings, ev["max_price"])
        self.db.set_state(ev["id"], d.low, d.listings, d.under_target)
        if d.kinds:
            res.alert_id = self.db.add_alert(ev["id"], d.kinds, alert_text(ev, d.lines, tm_now))
        return res


NICKNAME_TAILS = {"blazers", "sox", "jays", "knights", "wings", "city", "united", "fc"}


def search_queries(query: str) -> list[str]:
    """'Knicks Trail Blazers' -> ['Knicks Trail Blazers', 'Trail Blazers', 'Knicks', ...]: the full
    query, then each name. Two-word nicknames (Trail Blazers, Red Sox) stay together."""
    words = query.split()
    names: list[str] = []
    for w in words:
        if names and w.lower() in NICKNAME_TAILS:
            names[-1] += " " + w
        else:
            names.append(w)
    out = [query] + sorted(names, key=len, reverse=True)
    return list(dict.fromkeys(q for q in out if q.strip()))


def parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def links_html(ev: sqlite3.Row) -> str:
    out = []
    if ev["seatgeek_url"]:
        out.append(f'<a href="{html.escape(sg.buy_url(ev["seatgeek_url"], ev["quantity"]))}">'
                   f'SeatGeek ({ev["quantity"]} tickets)</a>')
    if ev["tm_url"]:
        out.append(f'<a href="{html.escape(ev["tm_url"])}">Ticketmaster</a>')
    return " · ".join(out)


def event_header(ev: sqlite3.Row) -> str:
    return (f"🎟 <b>{html.escape(ev['name'])}</b>\n{event_time(ev['starts_local'])}"
            + (f" · {html.escape(ev['venue'])}" if ev["venue"] else ""))


def alert_text(ev: sqlite3.Row, lines: list[str], tm_now: EventInfo | None = None) -> str:
    parts = [event_header(ev), *lines]
    if ev["quantity"] > 1:
        parts.append(f"<i>Lowest single-ticket price; check that {ev['quantity']} seats "
                     "together are available at it.</i>")
    if tm_now and tm_now.status and tm_now.status not in ("onsale",):
        parts.append(f"Ticketmaster status: {html.escape(tm_now.status)}")
    parts.append(links_html(ev))
    return "\n".join(p for p in parts if p)
