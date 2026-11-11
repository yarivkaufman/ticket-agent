"""Configuration: config.yaml (rules, schedule) + environment variables (secrets/paths)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Alerts(BaseModel):
    # a new low alerts when it beats the last announced low by at least this much (either one)
    min_drop_usd: float = 5.0
    min_drop_pct: float = 3.0
    # "new seats": the listing count grew by at least this many AND this percent since last announced
    min_new_listings: int = 10
    min_new_listings_pct: float = 10.0


class Schedule(BaseModel):
    check_every_minutes: int = 60  # normal pace
    close_hours: float = 48  # within this many hours of the event...
    close_every_minutes: int = 15  # ...check this often


class Parsing(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"


class Config(BaseModel):
    timezone: str = "America/New_York"
    web_base_url: str = "http://localhost:8098"
    default_quantity: int = 2
    # SeatGeek's lowest_price is the all-in price per ticket. If a check on seatgeek.com shows
    # fees on top, set this (e.g. 1.25) so totals and targets compare all-in prices.
    fee_multiplier: float = 1.0
    alerts: Alerts = Field(default_factory=Alerts)
    schedule: Schedule = Field(default_factory=Schedule)
    parsing: Parsing = Field(default_factory=Parsing)
    sources: dict[str, bool] = Field(default_factory=lambda: {"seatgeek": True, "ticketmaster": True})

    def source_on(self, name: str) -> bool:
        return self.sources.get(name, True)


class Settings(BaseModel):
    """Runtime settings from the environment."""

    data_dir: Path
    config_path: Path
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = Field(default=None, repr=False)
    email_from: str | None = None
    email_to: list[str] = Field(default_factory=list)  # price alerts
    admin_email: str | None = None  # maintenance/failure messages; empty = log + /runs only
    anthropic_configured: bool
    seatgeek_client_id: str | None = Field(default=None, repr=False)
    ticketmaster_key: str | None = Field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> Settings:
        env = lambda k: os.environ.get(k, "").strip() or None  # noqa: E731
        to = [a for a in (env("ALERT_EMAIL_TO") or "").replace(" ", "").split(",") if a]
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            config_path=Path(os.environ.get("CONFIG_PATH", "config.yaml")),
            smtp_host=env("SMTP_HOST"),
            smtp_port=int(env("SMTP_PORT") or 587),
            smtp_user=env("SMTP_USER"),
            smtp_password=env("SMTP_PASSWORD"),
            email_from=env("ALERT_EMAIL_FROM") or env("SMTP_USER"),
            email_to=to,
            admin_email=env("ADMIN_EMAIL"),
            anthropic_configured=bool(os.environ.get("ANTHROPIC_API_KEY")),
            seatgeek_client_id=os.environ.get("SEATGEEK_CLIENT_ID", "").strip() or None,
            ticketmaster_key=os.environ.get("TICKETMASTER_API_KEY", "").strip() or None,
        )


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    with open(path, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f) or {})
