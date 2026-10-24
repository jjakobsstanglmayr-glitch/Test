"""Reddit-Stimmung über die offizielle API (OAuth, App-only).

Benötigt eine kostenlose "script"-App unter https://www.reddit.com/prefs/apps.
Die kostenlose Stufe erlaubt 100 Anfragen pro Minute; wir bleiben deutlich darunter.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from news_scanner.config import WatchItem
from news_scanner.http import HttpClient

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
LISTING_URL = "https://oauth.reddit.com/r/{sub}/new"

# Kürzel, die als normales Wort zu häufig vorkommen, zählen nur als $Cashtag.
AMBIGUOUS = {"A", "ALL", "ARE", "BE", "CAN", "DD", "FOR", "IT", "NOW", "ON", "ONE", "OR", "SO", "YOLO", "CEO", "EU", "USA", "AI"}


class RedditClient:
    def __init__(self, http: HttpClient, client_id: str, client_secret: str, user_agent: str):
        if not client_id or not client_secret:
            raise ValueError("Reddit-Zugangsdaten fehlen (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET).")
        self.http = http
        self.user_agent = user_agent
        resp = http.post(
            TOKEN_URL,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": user_agent},
        )
        self.token = resp.json()["access_token"]

    def listing(self, sub: str, after: str | None = None) -> dict:
        params = {"limit": 100, "raw_json": 1}
        if after:
            params["after"] = after
        resp = self.http.get(
            LISTING_URL.format(sub=sub),
            params=params,
            headers={"Authorization": f"bearer {self.token}", "User-Agent": self.user_agent},
        )
        return resp.json()


def fetch_posts(listing_fn, sub: str, hours: float, max_pages: int) -> list[dict]:
    """Holt neue Beiträge, bis ``hours`` abgedeckt sind oder ``max_pages`` erreicht ist."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    posts: list[dict] = []
    after = None
    for _ in range(max_pages):
        data = listing_fn(sub, after)["data"]
        children = [c["data"] for c in data.get("children", [])]
        posts.extend(children)
        after = data.get("after")
        if not after or not children:
            break
        if datetime.fromtimestamp(children[-1]["created_utc"], tz=timezone.utc) < cutoff:
            break
    return posts


def mention_patterns(watchlist: list[WatchItem]) -> dict[str, re.Pattern]:
    patterns = {}
    for w in watchlist:
        sym = re.escape(w.reddit_ticker)
        parts = [rf"\${sym}\b"]
        if w.reddit_ticker not in AMBIGUOUS and len(w.reddit_ticker) >= 2:
            parts.append(rf"(?<![\w$]){sym}(?!\w)")
        # Kürzel nur in Großschreibung, Firmennamen unabhängig von Groß-/Kleinschreibung.
        parts += [rf"(?i:\b{re.escape(term)}\b)" for term in w.search_terms if len(term) >= 3]
        patterns[w.ticker] = re.compile("|".join(parts))
    return patterns


def count_mentions(posts: list[dict], watchlist: list[WatchItem], now: datetime | None = None) -> dict[str, dict]:
    """Zählt Beiträge mit Erwähnung je Ticker in den letzten 24 h und den 24 h davor."""
    now = now or datetime.now(timezone.utc)
    patterns = mention_patterns(watchlist)
    today: Counter = Counter()
    yesterday: Counter = Counter()
    oldest = now
    for post in posts:
        created = datetime.fromtimestamp(post["created_utc"], tz=timezone.utc)
        oldest = min(oldest, created)
        text = f"{post.get('title', '')} {post.get('selftext', '')[:2000]}"
        age = now - created
        bucket = today if age <= timedelta(hours=24) else yesterday if age <= timedelta(hours=48) else None
        if bucket is None:
            continue
        for ticker, pat in patterns.items():
            if pat.search(text):
                bucket[ticker] += 1
    covers_yesterday = now - oldest >= timedelta(hours=47)
    return {
        w.ticker: {"heute": today[w.ticker], "vortag": yesterday[w.ticker] if covers_yesterday else None}
        for w in watchlist
    }


def merge_history(counts: dict[str, dict], history_path: Path, today: str) -> dict[str, dict]:
    """Speichert die Tageswerte und füllt fehlende Vortageswerte aus der Historie."""
    history: dict = {}
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = {}
    previous_days = sorted(d for d in history if d < today)
    prev = history.get(previous_days[-1], {}) if previous_days else {}
    for ticker, c in counts.items():
        if c["vortag"] is None and ticker in prev:
            c["vortag"] = prev[ticker]
        c["veraenderung"] = None if c["vortag"] is None else c["heute"] - c["vortag"]
    history[today] = {t: c["heute"] for t, c in counts.items()}
    for old in sorted(history)[:-30]:
        del history[old]
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history, indent=1, sort_keys=True), encoding="utf-8")
    return counts
