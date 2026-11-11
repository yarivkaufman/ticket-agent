from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from ticket_agent.config import Config, Settings
from ticket_agent.db import DB, event_time
from ticket_agent.emailer import Emailer, to_plain
from ticket_agent.parser import EventRequest, Parser, best_match, fallback_parse
from ticket_agent.pipeline import Tracker, decide, is_drop, more_listings
from ticket_agent.sources import EventInfo, SourceError
from ticket_agent.sources import seatgeek as sg
from ticket_agent.sources import ticketmaster as tm

CFG = Config()


def sg_json(eid=17654321, low=150.0, count=800, when="2026-11-21T19:30:00"):
    return {"id": eid, "title": "Portland Trail Blazers at New York Knicks", "datetime_local": when,
            "datetime_utc": "2026-11-22T00:30:00", "url": f"https://seatgeek.com/x/{eid}",
            "venue": {"name": "Madison Square Garden", "city": "New York"},
            "stats": {"lowest_price": low, "average_price": 310, "highest_price": 2500,
                      "listing_count": count}}


def tm_json(eid="3B00612F", when="2026-11-21"):
    return {"id": eid, "name": "New York Knicks vs. Portland Trail Blazers",
            "url": f"https://www.ticketmaster.com/x/event/{eid}",
            "dates": {"start": {"localDate": when, "localTime": "19:30:00",
                                "dateTime": "2026-11-22T00:30:00Z"}, "status": {"code": "onsale"}},
            "priceRanges": [{"type": "standard", "currency": "USD", "min": 45.0, "max": 950.0}],
            "_embedded": {"venues": [{"name": "Madison Square Garden", "city": {"name": "New York"}}]}}


def fake_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- sources -------------------------------------------------------------------------------------

def test_seatgeek_parse_and_search_params():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"events": [sg_json()]})

    s = sg.SeatGeek("cid", client=fake_client(handler))
    [e] = s.search("Knicks Trail Blazers", "2026-11-21")
    assert seen["datetime_local.gte"] == "2026-11-21" and seen["datetime_local.lt"] == "2026-11-22"
    assert seen["client_id"] == "cid"
    assert (e.lowest, e.listings, e.date, e.venue) == (150.0, 800, "2026-11-21", "Madison Square Garden")
    assert e.starts_utc == "2026-11-22T00:30:00Z"


def test_seatgeek_bad_key_is_a_source_error():
    s = sg.SeatGeek("bad", client=fake_client(lambda r: httpx.Response(403)))
    with pytest.raises(SourceError, match="client id"):
        s.event("1")


def test_seatgeek_missing_stats():
    e = sg.parse_event({"id": 1, "title": "x", "stats": {}})
    assert e.lowest is None and e.listings is None


def test_urls():
    assert sg.event_id_from_url("https://seatgeek.com/trail-blazers-at-knicks-tickets/nba/2026-11-21-7-30-pm/17654321") == "17654321"
    assert sg.event_id_from_url("https://example.com/17654321") is None
    assert tm.event_id_from_url("https://www.ticketmaster.com/knicks-v-blazers/event/3B00612FABC") == "3B00612FABC"
    assert sg.buy_url("https://seatgeek.com/x/1", 3) == "https://seatgeek.com/x/1?quantity=3"


def test_ticketmaster_filters_to_local_date():
    def handler(req):
        return httpx.Response(200, json={"_embedded": {"events": [tm_json(), tm_json("OTHER", "2026-11-22")]}})

    [e] = tm.Ticketmaster("k", client=fake_client(handler)).search("Knicks", "2026-11-21")
    assert (e.id, e.lowest, e.highest, e.status) == ("3B00612F", 45.0, 950.0, "onsale")


# --- parsing -------------------------------------------------------------------------------------

def test_fallback_parse():
    r = fallback_parse("Knicks Vs Portland in Medison Squere garden November 21, 3 tickets, $120",
                       date(2026, 11, 10))
    assert r.date == "2026-11-21" and r.quantity == 3 and r.max_price_per_ticket == 120
    assert "Knicks" in r.search_query and "Portland" in r.search_query


def test_fallback_parse_rolls_year():
    assert fallback_parse("Rangers Jan 5", date(2026, 11, 10)).date == "2027-01-05"


def ev(id_, title, when, venue="Madison Square Garden"):
    return EventInfo("seatgeek", id_, title, when, None, venue, "New York", "u")


def test_best_match_prefers_date_and_title():
    req = EventRequest(search_query="Knicks Trail Blazers", date="2026-11-21",
                       venue_or_city="Madison Square Garden", quantity=3, max_price_per_ticket=120)
    events = [ev("a", "Knicks at Celtics", "2026-11-21T19:30:00", "TD Garden"),
              ev("b", "Portland Trail Blazers at New York Knicks", "2026-11-21T19:30:00"),
              ev("c", "Portland Trail Blazers at New York Knicks", "2027-02-01T19:30:00")]
    assert best_match(req, events).id == "b"
    assert best_match(req.model_copy(update={"date": "2026-12-25"}), events) is None


# --- alert rules ---------------------------------------------------------------------------------

def test_thresholds():
    assert is_drop(100, 95, CFG) and is_drop(100, 97, CFG) and not is_drop(100, 98, CFG)
    assert more_listings(100, 110, CFG) and not more_listings(100, 109, CFG)
    assert not more_listings(1000, 1050, CFG)  # +50 is only 5%


def st(low, listings, under=0):
    return {"low_announced": low, "listings_announced": listings, "under_target": under}


def test_first_check_sets_baseline_without_alert():
    d = decide(CFG, None, 150, 800, 120)
    assert d.kinds == [] and d.low == 150 and d.listings == 800


def test_first_check_under_target_alerts():
    d = decide(CFG, None, 110, 800, 120)
    assert d.kinds == ["target"] and d.under_target


def test_target_alerts_once_per_crossing():
    d = decide(CFG, st(150, 800), 118, 800, 120)
    assert d.kinds == ["target"]
    d2 = decide(CFG, st(d.low, d.listings, 1), 117, 800, 120)
    assert d2.kinds == []  # still under, only $1 lower: quiet
    d3 = decide(CFG, st(118, 800, 1), 100, 800, 120)
    assert d3.kinds == ["low"]  # further big drop while under target
    d4 = decide(CFG, st(100, 800, 1), 130, 800, 120)
    assert not d4.under_target and d4.kinds == []
    assert decide(CFG, st(100, 800, 0), 119, 800, 120).kinds == ["target"]  # re-crossing alerts


def test_new_low_and_more_listings():
    d = decide(CFG, st(150, 800), 140, 900, None)
    assert d.kinds == ["low", "listings"] and d.low == 140 and d.listings == 900


def test_listings_follow_decreases():
    d = decide(CFG, st(150, 800), 150, 500, None)
    assert d.kinds == [] and d.listings == 500
    assert decide(CFG, st(150, 500), 150, 560, None).kinds == ["listings"]


def test_price_rise_keeps_announced_low():
    d = decide(CFG, st(150, 800), 170, 800, None)
    assert d.kinds == [] and d.low == 150


# --- tracker end to end --------------------------------------------------------------------------

class FakeSG:
    label = "SeatGeek"

    def __init__(self):
        self.low, self.count = 150.0, 800

    def search(self, q, on=None, limit=10):
        return [sg.parse_event(sg_json(low=self.low, count=self.count))]

    def event(self, eid):
        return sg.parse_event(sg_json(low=self.low, count=self.count))


def test_tracker_add_and_check(tmp_path):
    db = DB(tmp_path / "t.db")
    fake = FakeSG()
    t = Tracker(CFG, db, Parser(CFG.parsing, use_claude=False), fake, None)
    eid = t.add("Knicks vs Portland at MSG Nov 21, 3 tickets, max $120")
    e = db.event(eid)
    assert e["quantity"] == 3 and e["max_price"] == 120 and e["seatgeek_id"] == "17654321"
    assert t.check(e).alert_id is None  # baseline
    fake.low = 115.0
    res = t.check(db.event(eid))
    assert res.alert_id is not None
    [a] = db.unsent_alerts()
    assert "target" in a["kinds"] and "$115" in a["text"] and "quantity=3" in a["text"]
    assert db.lowest_seen(eid) == 115.0


def test_tracker_due():
    t = Tracker(CFG, None, None, None, None)
    at = datetime(2026, 11, 10, 12, tzinfo=UTC)
    far = {"starts_utc": "2026-11-22T00:30:00Z", "last_scan_at": (at - timedelta(minutes=30)).isoformat()}
    assert not t.due(far, at)
    assert t.due({**far, "last_scan_at": (at - timedelta(minutes=58)).isoformat()}, at)
    close = {"starts_utc": (at + timedelta(hours=20)).isoformat(),
             "last_scan_at": (at - timedelta(minutes=15)).isoformat()}
    assert t.due(close, at)
    assert t.due({"starts_utc": None, "last_scan_at": None}, at)


def test_source_error_is_recorded(tmp_path):
    class Broken(FakeSG):
        def event(self, eid):
            raise SourceError("SeatGeek HTTP 500")

    db = DB(tmp_path / "t.db")
    t = Tracker(CFG, db, Parser(CFG.parsing, use_claude=False), FakeSG(), None)
    eid = t.add("Knicks Portland Nov 21")
    t.seatgeek = Broken()
    res = t.check(db.event(eid))
    assert res.errors == ["seatgeek: SeatGeek HTTP 500"] and db.event(eid)["last_error"]


# --- email + misc --------------------------------------------------------------------------------

def test_email_queue_waits_for_smtp(tmp_path):
    db = DB(tmp_path / "t.db")
    eid = db.add_event(name="X", quantity=2)
    db.add_alert(eid, ["low"], "📉 New low")
    s = Settings(data_dir=tmp_path, config_path=tmp_path / "c.yaml", anthropic_configured=False)
    assert Emailer(s, db, "http://x").send_pending_alerts() == 0
    assert len(db.unsent_alerts()) == 1


def test_email_sends_and_marks(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.db")
    eid = db.add_event(name="Knicks", quantity=2)
    db.add_alert(eid, ["target"], '✅ <b>$110</b>\n<a href="https://seatgeek.com/x">SeatGeek</a>')
    s = Settings(data_dir=tmp_path, config_path=tmp_path / "c.yaml", anthropic_configured=False,
                 smtp_host="smtp.example", smtp_user="a@x", smtp_password="pw",
                 email_from="a@x", email_to=["me@x"])
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): pass
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, m): sent.append(("msg", m["Subject"], m["To"]))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    assert Emailer(s, db, "http://x").send_pending_alerts() == 1
    assert sent[1] == ("msg", "🎟 Knicks: under your target", "me@x")
    assert db.unsent_alerts() == []


def test_to_plain():
    assert to_plain('<b>$1</b> <a href="https://a">SeatGeek</a> &amp;') == "$1 SeatGeek: https://a &"


def test_event_time():
    assert event_time("2026-11-21T19:30:00") == "Sat Nov 21, 7:30 PM"
    assert event_time(None) == "date TBA"


# --- search fallbacks and links (found on the first live run) ---------------------------------

def test_search_queries_split_names():
    from ticket_agent.pipeline import search_queries
    assert search_queries("Knicks Trail Blazers") == ["Knicks Trail Blazers", "Trail Blazers", "Knicks"]
    assert search_queries("Taylor Swift") == ["Taylor Swift", "Taylor", "Swift"]


class OneTeamSG(FakeSG):
    """Like SeatGeek: a two-team query finds nothing, one team finds the game."""

    def search(self, q, on=None, limit=10):
        return [] if " " in q and q != "Trail Blazers" else super().search(q, on, limit)


def test_add_falls_back_to_one_team(tmp_path):
    db = DB(tmp_path / "t.db")
    t = Tracker(CFG, db, Parser(CFG.parsing, use_claude=False), OneTeamSG(), None)
    e = db.event(t.add("Knicks vs Trail Blazers Nov 21, 3 tickets, max $120"))
    assert e["seatgeek_id"] == "17654321" and e["quantity"] == 3


def test_add_by_link_inside_text(tmp_path):
    seen = []

    class LinkSG(FakeSG):
        def event(self, eid):
            seen.append(eid)
            return super().event(eid)

        def search(self, q, on=None, limit=10):
            raise AssertionError("should use the link, not search")

    db = DB(tmp_path / "t.db")
    t = Tracker(CFG, db, Parser(CFG.parsing, use_claude=False), LinkSG(), None)
    url = "https://seatgeek.com/trail-blazers-at-knicks-tickets/11-21-2026-new-york-madison-square-garden/nba/18494860"
    e = db.event(t.add(f"{url} 3 tickets, max $120"))
    assert seen[0] == "18494860" and e["quantity"] == 3 and e["max_price"] == 120


def test_no_price_data_is_reported(tmp_path):
    class NoStats(FakeSG):
        def event(self, eid):
            e = super().event(eid)
            e.lowest = e.listings = None
            return e

    db = DB(tmp_path / "t.db")
    t = Tracker(CFG, db, Parser(CFG.parsing, use_claude=False), NoStats(), None)
    eid = t.add("Knicks Portland Nov 21")
    res = t.check(db.event(eid))
    assert res.errors == ["seatgeek: the API returned no price data for this event"]


def test_client_secret_is_sent():
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json=sg_json())

    sg.SeatGeek("cid", client=fake_client(handler), client_secret="sec").event("1")
    assert seen["client_id"] == "cid" and seen["client_secret"] == "sec"
