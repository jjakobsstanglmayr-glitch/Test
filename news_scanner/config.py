"""Laden von config.yaml und positionen.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class WatchItem:
    ticker: str
    name: str
    aliases: list[str] = field(default_factory=list)
    isin: str = ""
    land: str = "US"
    sec_ticker: str = ""  # Ticker an der US-Börse für EDGAR (Standard: ticker)
    reddit_ticker: str = ""  # Kürzel, das auf Reddit verwendet wird

    @property
    def search_terms(self) -> list[str]:
        terms = [self.name, *self.aliases]
        return [t for t in dict.fromkeys(terms) if t]


@dataclass
class Position:
    ticker: str
    stueck: float
    einstieg: float
    stop: float | None = None
    datum: str = ""
    notiz: str = ""


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(path: str | Path) -> dict[str, Any]:
    cfg = load_yaml(path)
    cfg["watchlist"] = [_watch_item(w) for w in cfg.get("watchlist", [])]
    # Geheimnisse dürfen per Umgebungsvariable statt in der Datei gesetzt werden.
    reddit = cfg.setdefault("quellen", {}).setdefault("reddit", {})
    reddit["client_id"] = os.environ.get("REDDIT_CLIENT_ID") or reddit.get("client_id", "")
    reddit["client_secret"] = os.environ.get("REDDIT_CLIENT_SECRET") or reddit.get("client_secret", "")
    sec = cfg["quellen"].setdefault("sec", {})
    sec["user_agent"] = os.environ.get("SEC_USER_AGENT") or sec.get("user_agent", "")
    return cfg


def _watch_item(raw: dict[str, Any]) -> WatchItem:
    ticker = str(raw["ticker"]).upper()
    return WatchItem(
        ticker=ticker,
        name=raw.get("name", ticker),
        aliases=list(raw.get("aliases", [])),
        isin=raw.get("isin", ""),
        land=raw.get("land", "US").upper(),
        sec_ticker=str(raw.get("sec_ticker", ticker if raw.get("land", "US").upper() == "US" else "")).upper(),
        reddit_ticker=str(raw.get("reddit_ticker", ticker.split(".")[0])).upper(),
    )


def load_positions(path: str | Path) -> list[Position]:
    if not Path(path).exists():
        return []
    raw = load_yaml(path).get("positionen") or []
    return [
        Position(
            ticker=str(p["ticker"]).upper(),
            stueck=float(p.get("stueck", 0)),
            einstieg=float(p["einstieg"]),
            stop=float(p["stop"]) if p.get("stop") is not None else None,
            datum=str(p.get("datum", "")),
            notiz=p.get("notiz", ""),
        )
        for p in raw
    ]
