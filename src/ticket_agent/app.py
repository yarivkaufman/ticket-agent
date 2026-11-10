"""The long-running service: dashboard + scheduler (+ email alerts) in one asyncio process."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .chart import history_svg
from .config import Config, Settings, load_config
from .db import DB, event_time, local_time
from .emailer import Emailer
from .parser import Parser
from .pipeline import Tracker, money, parse_utc
from .sources import seatgeek as sg
from .sources import ticketmaster as tm

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["money"] = money
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
templates.env.filters["eventtime"] = event_time
templates.env.globals["buy_url"] = sg.buy_url


def build_sources(settings: Settings) -> tuple[sg.SeatGeek | None, tm.Ticketmaster | None]:
    return (sg.SeatGeek(settings.seatgeek_client_id) if settings.seatgeek_client_id else None,
            tm.Ticketmaster(settings.ticketmaster_key) if settings.ticketmaster_key else None)


class Agent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = DB(settings.data_dir / "tickets.db")
        self.seatgeek, self.ticketmaster = build_sources(settings)
        self.email = Emailer(settings, self.db, self.config().web_base_url)
        self.lock = asyncio.Lock()
        self.last_message = ""
        self.adding: list[str] = []
        self.fail_streak = 0  # consecutive runs where every SeatGeek check failed

    def config(self) -> Config:
        return load_config(self.settings.config_path)  # re-read each run: edit without restart

    def tracker(self) -> Tracker:
        cfg = self.config()
        return Tracker(cfg, self.db, Parser(cfg.parsing, self.settings.anthropic_configured),
                       self.seatgeek, self.ticketmaster)

    async def tick(self, force: bool = False) -> str:
        """Every 15 minutes: check the events that are due (hourly, or 15-min close to the event)."""
        if self.lock.locked():
            return "A check is already running."
        async with self.lock:
            at = datetime.now(UTC)
            tracker = self.tracker()
            events = []
            for ev in self.db.events(active=True):
                start = parse_utc(ev["starts_utc"])
                if start and start < at:
                    self.db.update_event(ev["id"], active=0)  # the event has started: stop
                    continue
                if force or tracker.due(ev, at):
                    events.append(ev)
            if not events:
                return "Nothing due."
            return await self._check(tracker, events)

    async def _check(self, tracker: Tracker, events: list) -> str:
        run_id = self.db.start_run()
        errors: list[str] = []
        alerts = sg_fail = 0
        try:
            for ev in events:
                res = await asyncio.to_thread(tracker.check, ev)
                alerts += res.alert_id is not None
                errors += [f"{ev['name']}: {e}" for e in res.errors]
                sg_fail += any(e.startswith("seatgeek:") for e in res.errors)
            await asyncio.to_thread(self.email.send_pending_alerts)
            msg = f"Checked {len(events)} events, {alerts} alerts" + (f", {len(errors)} errors" if errors else "")
            self.fail_streak = self.fail_streak + 1 if sg_fail == len(events) else 0
            if self.fail_streak == 3:
                await asyncio.to_thread(self.email.send_admin,
                                        "SeatGeek failed for every event 3 checks in a row - see /runs")
        except Exception as e:
            log.exception("check failed")
            msg = f"Check failed: {e}"
            errors.append(msg)
            await asyncio.to_thread(self.email.send_admin, msg)
        self.db.finish_run(run_id, len(events), alerts, errors)
        self.last_message = msg
        return msg

    async def add_event(self, text: str) -> str:
        self.adding.append(text)
        try:
            tracker = self.tracker()
            try:
                event_id = await asyncio.to_thread(tracker.add, text)
            except Exception as e:
                log.warning("adding %r failed: %s", text, e)
                self.last_message = f"Couldn't add “{text}”: {e}"
                return self.last_message
            ev = self.db.event(event_id)
            async with self.lock:
                await self._check(tracker, [ev])
            ev = self.db.event(event_id)
            now = self.db.latest(event_id).get("seatgeek")
            price = f"lowest now {money(now['lowest'])}" if now and now["lowest"] is not None else "no price yet"
            self.last_message = f"Tracking {ev['name']} ({event_time(ev['starts_local'])}): {price}."
            return self.last_message
        finally:
            self.adding.remove(text)

    async def check_one(self, event_id: int) -> None:
        ev = self.db.event(event_id)
        if ev is not None:
            async with self.lock:
                await self._check(self.tracker(), [ev])


def parse_money(value: str) -> float | None:
    value = value.replace("$", "").replace(",", "").strip()
    try:
        return round(float(value), 2) if value else None
    except ValueError:
        raise HTTPException(400, "price must be a number")


def create_app(settings: Settings | None = None) -> FastAPI:
    agent = Agent(settings or Settings.from_env())
    tz = agent.config().timezone
    templates.env.filters["localtime"] = lambda s: local_time(s, tz)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cfg = agent.config()
        sched = AsyncIOScheduler(timezone=cfg.timezone)
        sched.add_job(agent.tick, CronTrigger(minute="2,17,32,47", timezone=cfg.timezone),
                      max_instances=1, coalesce=True)
        sched.start()
        sources = [n for n, s in (("SeatGeek", agent.seatgeek), ("Ticketmaster", agent.ticketmaster)) if s]
        log.info("ticket-agent up; sources %s; email %s", ", ".join(sources) or "NONE",
                 "on" if agent.email.configured else "off (alerts queue until SMTP is set)")
        yield
        sched.shutdown(wait=False)

    app = FastAPI(title="Ticket Agent", lifespan=lifespan)
    app.state.agent = agent

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str = "active"):
        events = agent.db.events(active=view != "past")
        last_run = agent.db.last_run()
        return templates.TemplateResponse(request, "index.html", {
            "events": events, "latest": {e["id"]: agent.db.latest(e["id"]) for e in events},
            "view": view, "n_active": len(agent.db.events(True)), "n_past": len(agent.db.events(False)),
            "adding": list(agent.adding), "running": agent.lock.locked(),
            "last_message": agent.last_message, "last_run": last_run,
            "last_errors": json.loads(last_run["errors"] or "[]") if last_run else [],
            "email_on": agent.email.configured, "fee": agent.config().fee_multiplier,
            "sources": [n for n, s in (("SeatGeek", agent.seatgeek), ("Ticketmaster", agent.ticketmaster)) if s],
        })

    @app.post("/events")
    async def add(text: str = Form(...)):
        if text.strip():
            asyncio.create_task(agent.add_event(text.strip()))
        return RedirectResponse("/", status_code=303)

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event(request: Request, event_id: int):
        ev = agent.db.event(event_id)
        if ev is None:
            raise HTTPException(404)
        return templates.TemplateResponse(request, "event.html", {
            "ev": ev, "latest": agent.db.latest(event_id), "lowest_seen": agent.db.lowest_seen(event_id),
            "alerts": agent.db.alerts(event_id, 30), "fee": agent.config().fee_multiplier,
            "chart": history_svg(agent.db.history(event_id), ev["max_price"], tz,
                                 agent.config().fee_multiplier),
        })

    @app.post("/events/{event_id}/settings")
    def settings_(event_id: int, name: str = Form(...), quantity: int = Form(...),
                  max_price: str = Form(""), seatgeek_id: str = Form(""), tm_id: str = Form("")):
        ev = agent.db.event(event_id)
        if ev is None:
            raise HTTPException(404)
        target = parse_money(max_price)
        agent.db.update_event(event_id, name=name.strip() or ev["name"], quantity=max(1, quantity),
                              max_price=target, seatgeek_id=seatgeek_id.strip() or None,
                              tm_id=tm_id.strip() or None)
        if target != ev["max_price"] or (seatgeek_id.strip() or None) != ev["seatgeek_id"]:
            agent.db.reset_state(event_id)  # new target / event: start announcing afresh
        return RedirectResponse(f"/events/{event_id}", status_code=303)

    @app.post("/events/{event_id}/active")
    def set_active(event_id: int, active: int = Form(...)):
        if not agent.db.update_event(event_id, active=int(bool(active))):
            raise HTTPException(404)
        return RedirectResponse(f"/events/{event_id}", status_code=303)

    @app.post("/events/{event_id}/delete")
    def delete(event_id: int):
        agent.db.delete_event(event_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/events/{event_id}/check")
    async def check_event(event_id: int):
        asyncio.create_task(agent.check_one(event_id))
        return RedirectResponse(f"/events/{event_id}", status_code=303)

    @app.post("/run")
    async def trigger():
        asyncio.create_task(agent.tick(force=True))
        return RedirectResponse("/", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        return templates.TemplateResponse(request, "runs.html", {"runs": agent.db.recent_runs()})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "running": agent.lock.locked()}

    return app
