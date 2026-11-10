# Ticket Agent

Tracks the cheapest ticket price for games and shows and emails you when it drops to your
target, hits a new low, or more seats get listed. One Docker service: dashboard + scheduler.

```
Add an event on the dashboard: "Knicks vs Portland at MSG Nov 21, 3 tickets, max $120"
(or paste a SeatGeek / Ticketmaster event link)
  -> Claude reads the request (teams, date, ticket count, max price per ticket)
  -> finds the event on SeatGeek (and Ticketmaster) -> first check right away
Every hour (every 15 min in the last 48 h before the event):
  SeatGeek: cheapest ticket (all-in per ticket) + listing count
  Ticketmaster: face-value range + sale status (info only)
  -> email on: at/under your target, a new low ($5 or 3% below the last announced low),
     more seats listed (+10 listings and +10%)
Events stop being checked once they start.
```

## Limits (by design)

Only free, official APIs are used. No ticket site lets an individual read seat-level listings
(sections, rows, seats together) through an official route, and the agent never scrapes around
bot protection. So prices are the **cheapest single ticket** for the event; when you need
several tickets, open the SeatGeek link (quantity preselected) to see seats together.

## Run

```
cp .env.example .env            # API keys + SMTP (server only)
mkdir -p config && cp config.example/config.yaml config/
docker compose up -d --build    # dashboard on :8097
docker compose exec ticket-agent ticket-agent test-email
```

CLI (inside the container: `docker compose exec ticket-agent ticket-agent ...`):
`check` (all events now) · `add "<request or link>"` · `probe "<query>" [--date YYYY-MM-DD]`
(search both APIs) · `test-email`.

## Develop

`uv sync`, then `uv run python -m pytest -q`. Run locally with
`DATA_DIR=<tmp> CONFIG_PATH=config.example/config.yaml uv run python -m ticket_agent serve`.
Without `ANTHROPIC_API_KEY` a simple regex reader replaces Claude for adding events.
