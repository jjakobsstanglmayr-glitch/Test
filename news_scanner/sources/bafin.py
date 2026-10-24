"""Directors' Dealings (Art. 19 MAR) aus der öffentlichen BaFin-Datenbank.

Die Datenbank (https://portal.mvp.bafin.de/database/DealingsInfo/) bietet für jede
Suche einen CSV-Export an. Wir fragen pro ISIN der Watchlist ab und werten die CSV
aus. Die Spalten werden über ihre Überschrift erkannt, damit kleine Formatänderungen
nicht sofort zum Ausfall führen.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone

from news_scanner.config import WatchItem
from news_scanner.http import HttpClient
from news_scanner.models import InsiderTrade
from news_scanner.sources.sec import classify_role

log = logging.getLogger(__name__)

DEFAULT_URL = (
    "https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do"
    "?meldepflichtigerName=&zeitraum=0&emittentButton=Suche+Emittent&emittentName="
    "&zeitraumVon=&zeitraumBis=&emittentIsin={isin}&d-4000784-e=1&6578706f7274=1"
)
WEB_URL = "https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do?emittentIsin={isin}&emittentButton=Suche+Emittent"

COLUMNS = {
    "emittent": ("emittent",),
    "isin": ("isin",),
    "name": ("meldepflichtig",),
    "position": ("position",),
    "instrument": ("art des instruments", "instrument"),
    "art": ("art des geschäfts", "art des geschaefts", "geschäftsart"),
    "preis": ("durchschnittspreis", "preis"),
    "volumen": ("aggregiertes volumen", "volumen"),
    "datum": ("datum des geschäfts", "datum des geschaefts", "geschäftsdatum"),
    "mitteilung": ("mitteilungsdatum",),
}


def parse_de_number(raw: str) -> tuple[float, str]:
    """'1.234,56 EUR' -> (1234.56, 'EUR')."""
    raw = (raw or "").strip()
    currency = "".join(re.findall(r"[A-Z]{3}", raw)) or "EUR"
    num = re.sub(r"[^0-9,.\-]", "", raw)
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    try:
        return float(num), currency[:3]
    except ValueError:
        return 0.0, currency[:3]


def _parse_date(raw: str) -> datetime | None:
    raw = (raw or "").strip()[:10]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _column_map(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [h.strip().lower() for h in header]
    for key, candidates in COLUMNS.items():
        for idx, col in enumerate(lowered):
            if any(c in col for c in candidates) and idx not in mapping.values():
                mapping[key] = idx
                break
    return mapping


def parse_csv(text: str, ticker: str, link: str) -> list[InsiderTrade]:
    text = text.lstrip("﻿")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t") if text.strip() else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    cols = _column_map(rows[0])
    missing = {"name", "art", "volumen"} - cols.keys()
    if missing:
        raise ValueError(f"BaFin-CSV: Spalten nicht erkannt ({', '.join(sorted(missing))})")
    trades = []
    for row in rows[1:]:
        if len(row) < len(rows[0]):
            continue

        def get(key: str) -> str:
            return row[cols[key]].strip() if key in cols else ""

        art = get("art").lower()
        tx_type = "Kauf" if "kauf" in art and "verkauf" not in art else "Verkauf" if "verkauf" in art else "Sonstiges"
        value, currency = parse_de_number(get("volumen"))
        price, _ = parse_de_number(get("preis"))
        role, top = classify_role(get("position"), False, False, False)
        trades.append(
            InsiderTrade(
                ticker=ticker,
                company=get("emittent"),
                insider=get("name"),
                role=role,
                is_ceo_cfo=top,
                tx_type=tx_type,
                tx_code=get("art"),
                date=_parse_date(get("datum")) or _parse_date(get("mitteilung")),
                shares=value / price if price else 0.0,
                price=price,
                value=value,
                currency=currency,
                link=link,
                source="BaFin",
            )
        )
    return trades


def fetch_dealings(
    http: HttpClient, watchlist: list[WatchItem], days: int, url_template: str = DEFAULT_URL
) -> tuple[list[InsiderTrade], list[str]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    trades: list[InsiderTrade] = []
    errors: list[str] = []
    for w in watchlist:
        if not w.isin or w.land != "DE":
            continue
        try:
            text = http.get_text(url_template.format(isin=w.isin))
            found = parse_csv(text, w.ticker, WEB_URL.format(isin=w.isin))
            trades.extend(t for t in found if t.date is None or t.date >= since)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{w.ticker}: {exc}")
    return trades, errors
