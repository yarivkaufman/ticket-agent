"""ticket-agent: tracks the cheapest ticket price for games and shows and emails alerts."""

from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    p = argparse.ArgumentParser(prog="ticket-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="dashboard + scheduler + email alerts")
    sub.add_parser("check", help="check every active event once, then exit")
    add = sub.add_parser("add", help="add an event (description or SeatGeek/Ticketmaster link), check it, exit")
    add.add_argument("text")
    probe = sub.add_parser("probe", help="search both sources and print what they found")
    probe.add_argument("query")
    probe.add_argument("--date", help="YYYY-MM-DD")
    sub.add_parser("test-email", help="send a test email to ALERT_EMAIL_TO")
    args = p.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs carry the API keys

    from .config import Settings

    settings = Settings.from_env()
    if args.cmd == "serve":
        import uvicorn

        from .app import create_app
        uvicorn.run(create_app(settings), host="0.0.0.0", port=int(os.environ.get("PORT", "8097")))
        return

    from .app import Agent
    agent = Agent(settings)
    if args.cmd == "probe":
        for src in (agent.seatgeek, agent.ticketmaster):
            if src is None:
                continue
            for e in src.search(args.query, args.date):
                print(f"{src.label:<12} {e.starts_local or '?':<19} low={e.lowest} listings={e.listings} "
                      f"status={e.status}  {e.title[:50]} @ {e.venue}  id={e.id}")
        return
    if args.cmd == "test-email":
        ok = agent.email.configured and agent.email.send(
            settings.email_to, "ticket-agent test", "Email alerts from ticket-agent work.")
        raise SystemExit(0 if ok else "email not sent: check SMTP_* / ALERT_EMAIL_* in .env and the log")

    import asyncio
    if args.cmd == "add":
        print(asyncio.run(agent.add_event(args.text)))
    else:
        print(asyncio.run(agent.tick(force=True)))
    for a in agent.db.alerts(limit=5):
        if a["sent_at"] is None:
            print("\n[queued alert]\n" + a["text"])
