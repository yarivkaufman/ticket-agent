# Ticket Agent

Finds the cheapest ticket price for a game/show and emails Yariv when it reaches his target,
drops, or more seats get listed. See README.md for how it works.

## Decided with Yariv (2026-11-10)
- Fully separate from price-agent and job-agent: own repo, own container, own DB, no shared
  code or bots. price-agent was only the reference for stack and conventions.
- Sources: "whatever is doable". Only free official APIs, never scraping or getting around bot
  protection. SeatGeek Platform API (cheapest price + listing count per event) is what's tracked;
  Ticketmaster Discovery API adds face value + sale status (info only, not alerted on).
  Seat-level data (sections/rows/seats together) isn't available to individuals: StubHub's API
  covers a seller's own listings, Ticketmaster resale listings are partner-only, Vivid Seats /
  TickPick / Gametime have no listing API. Chosen: cheapest price per event (decision card).
- First event: Knicks vs Portland Trail Blazers, Madison Square Garden, Nov 21 2026;
  3 tickets, max $120 per ticket all-in.
- Checks: every hour, every 15 minutes in the last 2 days (config.yaml `schedule`).
- Alerts: email only, Telegram left out for now. To yariv@kaufmanweb.net, sent from a separate
  mailbox for the agent (not Yariv's own account), via SMTP + app password in the server .env.
- Hosting: same Linux server as price-agent (192.168.1.67), ~/ticket-agent, port 8098 (8097 is taken by flight-finder).
  Outside access wanted, like price-agent (Cloudflare Tunnel + Access); not set up yet.

## Working rules
- Confirm with Yariv before deploying to or changing anything on the server or Cloudflare.
- No secrets in the repo or in OneDrive: API keys and the SMTP password live only in the server
  `.env`. Never log request URLs from httpx (they carry the API keys); emailer logs no credentials.
- Tests: `uv run python -m pytest -q`.

## Open items
- Verify on the first live run that SeatGeek's `lowest_price` matches the all-in checkout price
  on seatgeek.com; if not, set `fee_multiplier` in config.yaml.
- Deploy, Cloudflare hostname + Access app, keys (see the deploy checklist in the PR).
