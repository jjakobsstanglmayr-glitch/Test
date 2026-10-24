"""Nachrichten und Ad-hoc-Meldungen per RSS (inkl. Google News pro Ticker)."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser

from news_scanner.config import WatchItem
from news_scanner.http import HttpClient
from news_scanner.models import NewsItem, SourceStatus

log = logging.getLogger(__name__)

GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={gl}:{lang}"
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


def _published(entry) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)


def parse_feed(text: str, source: str, kind: str = "news") -> list[NewsItem]:
    feed = feedparser.parse(text)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Feed nicht lesbar: {feed.bozo_exception}")
    items = []
    for entry in feed.entries:
        title = _clean(entry.get("title", ""))
        if not title:
            continue
        publisher = ""
        src = entry.get("source")
        if src and src.get("title"):
            publisher = src["title"]
        # Google News hängt den Herausgeber als " - Reuters" an den Titel an.
        if source.startswith("Google News") and " - " in title:
            title, _, suffix = title.rpartition(" - ")
            publisher = publisher or suffix
        items.append(
            NewsItem(
                title=title,
                link=entry.get("link", ""),
                source=source,
                published=_published(entry),
                summary=_clean(entry.get("summary", ""))[:600],
                publisher=publisher or source,
                kind=kind,
            )
        )
    return items


def fetch_feeds(
    http: HttpClient, feeds: list[dict], max_age_hours: float, kind: str = "news"
) -> tuple[list[NewsItem], list[SourceStatus]]:
    """Lädt eine Liste von Feeds (``name``, ``url``, optional ``aktiv``); Fehler je Feed werden abgefangen."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items: list[NewsItem] = []
    statuses: list[SourceStatus] = []
    for feed in feeds:
        if not feed.get("aktiv", True):
            continue
        name = feed["name"]
        try:
            parsed = parse_feed(http.get_text(feed["url"]), name, kind=feed.get("art", kind))
            fresh = [i for i in parsed if i.published is None or i.published >= cutoff]
            items.extend(fresh)
            statuses.append(SourceStatus(name, True, len(fresh)))
        except Exception as exc:  # noqa: BLE001 - eine defekte Quelle darf den Lauf nicht stoppen
            log.warning("Feed %s fehlgeschlagen: %s", name, exc)
            statuses.append(SourceStatus(name, False, error=str(exc)[:200]))
    return items, statuses


def google_news_feeds(watchlist: list[WatchItem], cfg: dict) -> list[dict]:
    """Erzeugt pro Watchlist-Eintrag einen Google-News-RSS-Feed (DE-Werte auf Deutsch)."""
    feeds = []
    extra = cfg.get("zusatz_suchbegriff", "Aktie OR stock")
    for w in watchlist:
        german = w.land == "DE"
        hl, gl, lang = ("de", "DE", "de") if german else ("en-US", "US", "en")
        query = f'"{w.name}" ({extra})' if extra else f'"{w.name}"'
        url = GOOGLE_NEWS_URL.format(query=quote_plus(query), hl=hl, gl=gl, lang=lang)
        feeds.append({"name": f"Google News {w.ticker}", "url": url, "ticker": w.ticker})
    for site in cfg.get("seiten", []):
        # Z. B. Reuters, das keine eigenen RSS-Feeds mehr anbietet.
        url = GOOGLE_NEWS_URL.format(
            query=quote_plus(f"site:{site['domain']} {site.get('suche', 'stocks')} when:1d"),
            hl="en-US", gl="US", lang="en",
        )
        feeds.append({"name": f"Google News {site['name']}", "url": url})
    return feeds


def fetch_google_news(
    http: HttpClient, watchlist: list[WatchItem], cfg: dict, max_age_hours: float
) -> tuple[list[NewsItem], list[SourceStatus]]:
    feeds = google_news_feeds(watchlist, cfg)
    items: list[NewsItem] = []
    statuses: list[SourceStatus] = []
    for feed in feeds:
        got, st = fetch_feeds(http, [feed], max_age_hours)
        if feed.get("ticker"):
            for item in got:
                item.tickers.add(feed["ticker"])
        items.extend(got)
        statuses.extend(st)
    return items, statuses
