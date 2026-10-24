"""Gemeinsame Datenstrukturen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsItem:
    title: str
    link: str
    source: str  # Name der Quelle laut config, z. B. "CNBC"
    published: datetime | None
    summary: str = ""
    publisher: str = ""  # tatsächlicher Herausgeber (bei Google News aus dem Titel)
    tickers: set[str] = field(default_factory=set)
    kind: str = "news"  # news | adhoc | 8-K


@dataclass
class InsiderTrade:
    ticker: str
    company: str
    insider: str
    role: str  # z. B. "CEO", "CFO", "Director", "Vorstand"
    is_ceo_cfo: bool
    tx_type: str  # "Kauf" | "Verkauf" | "Sonstiges"
    tx_code: str  # SEC-Code (P, S, ...) bzw. BaFin-Geschäftsart
    date: datetime | None
    shares: float
    price: float
    value: float
    currency: str
    link: str
    source: str  # "SEC Form 4" | "BaFin"


@dataclass
class SourceStatus:
    name: str
    ok: bool
    items: int = 0
    error: str = ""


@dataclass
class Technicals:
    close: float
    prev_close: float
    change_pct: float
    rsi: float
    sma20: float
    sma50: float
    atr: float
    volume_ratio: float  # letztes Volumen / Ø 20 Tage
    high_20: float
    low_20: float
    currency: str = ""


@dataclass
class TradePlan:
    entry_low: float
    entry_high: float
    stop: float
    target1: float
    target2: float
    exit_rules: list[str]


@dataclass
class Candidate:
    ticker: str
    name: str
    direction: str  # "Long" | "Short" | "Neutral"
    score: int
    catalysts: list[str]
    headline: str
    summary_lines: list[str]
    links: list[tuple[str, str]]  # (Titel, URL)
    credibility: float
    rumor: bool
    reaction: str
    technicals: Technicals | None
    tech_notes: list[str]
    plan: TradePlan | None
    score_notes: list[str]
