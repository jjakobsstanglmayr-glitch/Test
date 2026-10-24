"""Demo-Modus: liefert Beispieldaten im Originalformat der Quellen, ganz ohne Netzwerk.

Alle Unternehmen, Meldungen und Kurse sind frei erfunden. Die Fixtures enthalten
Zeit-Platzhalter wie ``{{RFC:-3h}}``, die relativ zur aktuellen Uhrzeit ersetzt
werden, damit der Demo-Bericht immer "frisch" ist.
"""

from __future__ import annotations

import json
import re
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote_plus

import numpy as np
import pandas as pd

from news_scanner.http import HttpError

FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_RSS = '<?xml version="1.0"?><rss version="2.0"><channel><title>leer</title></channel></rss>'

_PLACEHOLDER = re.compile(r"\{\{(RFC|ISO|DE|ISOTS):([+-]\d+)([hd])\}\}")


def render_placeholders(text: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)

    def repl(m: re.Match) -> str:
        kind, amount, unit = m.group(1), int(m.group(2)), m.group(3)
        t = now + (timedelta(hours=amount) if unit == "h" else timedelta(days=amount))
        if kind == "RFC":
            return t.strftime("%a, %d %b %Y %H:%M:%S +0000")
        if kind == "ISO":
            return t.strftime("%Y-%m-%d")
        if kind == "DE":
            return t.strftime("%d.%m.%Y")
        return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return _PLACEHOLDER.sub(repl, text)


class _Response:
    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = 200

    def json(self):
        return json.loads(self.text)


class DemoHttp:
    """Ersetzt den HttpClient und beantwortet Anfragen aus ``fixtures/``."""

    ROUTES = [
        (r"cnbc\.com", "rss_cnbc.xml"),
        (r"dowjones\.io", "rss_marketwatch.xml"),
        (r"finance\.yahoo\.com/news/rssindex", "rss_yahoo.xml"),
        (r"handelsblatt\.com", "rss_handelsblatt.xml"),
        (r"finanzen\.net", "rss_finanzen.xml"),
        (r"eqs-news\.com", "rss_eqs.xml"),
        (r"sec\.gov/files/company_tickers\.json", "sec_company_tickers.json"),
        (r"browse-edgar\?action=getcurrent", "sec_current_form4.atom"),
    ]

    def __init__(self, now: datetime | None = None):
        self.now = now or datetime.now(timezone.utc)
        self.requests: list[str] = []

    def _load(self, name: str) -> str:
        return render_placeholders((FIXTURES / name).read_text(encoding="utf-8"), self.now)

    def get_text(self, url: str, cache_hours: float = 0, **kwargs) -> str:
        self.requests.append(url)
        if "investing.com" in url:
            # Absichtlich: zeigt, dass ein Ausfall einer Quelle abgefangen wird.
            raise HttpError(f"HTTP 503 für {url}")
        for pattern, name in self.ROUTES:
            if re.search(pattern, url):
                return self._load(name)
        if "news.google.com" in url:
            query = unquote_plus(url)
            for path in sorted(FIXTURES.glob("google_*.xml")):
                key = path.stem.removeprefix("google_").replace("_", " ")
                if key.lower() in query.lower():
                    return self._load(path.name)
            return EMPTY_RSS
        if m := re.search(r"submissions/CIK(\d+)\.json", url):
            path = FIXTURES / f"sec_submissions_{int(m.group(1))}.json"
            return self._load(path.name) if path.exists() else '{"filings": {"recent": {}}}'
        if m := re.search(r"Archives/edgar/data/\d+/(\d+)/index\.json", url):
            return json.dumps({"directory": {"item": [{"name": f"form4_{m.group(1)}.xml"}]}})
        if m := re.search(r"Archives/edgar/data/\d+/(\d+)/([^/]+)$", url):
            path = FIXTURES / f"form4_{m.group(1)}.xml"
            if path.exists():
                return self._load(path.name)
            raise HttpError(f"HTTP 404 für {url}")
        if m := re.search(r"bafin\.de.*emittentIsin=([A-Z0-9]+)", url):
            path = FIXTURES / f"bafin_{m.group(1)}.csv"
            return self._load(path.name) if path.exists() else self._load("bafin_leer.csv")
        raise HttpError(f"HTTP 404 (keine Demo-Daten) für {url}")

    def get_json(self, url: str, cache_hours: float = 0, **kwargs):
        return json.loads(self.get_text(url, **kwargs))

    def get(self, url: str, **kwargs) -> _Response:
        return _Response(self.get_text(url))


# ------------------------------------------------------------------ Kurse

# Ticker: (Startkurs, Tagesvolatilität, Trend pro Tag, Bewegung am letzten Tag in %, Volumen-Faktor letzter Tag)
PRICE_SPECS = {
    "HLXT": (38.0, 0.022, 0.0005, 13.5, 4.2),
    "QGRD": (61.0, 0.018, 0.0022, 1.4, 1.8),
    "NWE.DE": (24.0, 0.015, 0.0012, 0.4, 1.1),
    "APXC": (112.0, 0.020, -0.0003, 2.1, 2.6),
    "BVR.DE": (88.0, 0.017, -0.0015, -6.8, 3.5),
    "CDLB": (17.0, 0.012, 0.0004, 0.6, 1.3),
}


class DemoMarket:
    """Erzeugt reproduzierbare synthetische Tageskerzen (Zufallspfad je Ticker)."""

    def __init__(self, now: datetime | None = None, days: int = 130):
        self.now = now or datetime.now(timezone.utc)
        self.days = days

    def history(self, ticker: str) -> tuple[pd.DataFrame, str]:
        start, vol, drift, last_move, last_vol = PRICE_SPECS.get(ticker, (50.0, 0.018, 0.0, 0.0, 1.0))
        rng = np.random.default_rng(zlib.crc32(ticker.encode()))
        end = pd.Timestamp(self.now.date())
        idx = pd.bdate_range(end=end, periods=self.days)
        rets = rng.normal(drift, vol, self.days)
        rets[-1] = last_move / 100
        close = start * np.exp(np.cumsum(np.log1p(rets)))
        opens = np.r_[start, close[:-1]] * (1 + rng.normal(0, vol / 4, self.days))
        spread = np.abs(rng.normal(0, vol * 0.6, self.days)) + vol * 0.3
        high = np.maximum(opens, close) * (1 + spread / 2)
        low = np.minimum(opens, close) * (1 - spread / 2)
        volume = rng.lognormal(13.5, 0.25, self.days)
        volume[-1] = volume[-21:-1].mean() * last_vol
        df = pd.DataFrame({"Open": opens, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)
        return df, "EUR" if ticker.endswith(".DE") else "USD"


# ------------------------------------------------------------------ Reddit

# (Subreddit, Titel, Stunden zurück, Anzahl Wiederholungen)
REDDIT_POSTS = [
    ("wallstreetbets", "APXC takeover rumors - $APXC calls printing?", 3, 9),
    ("stocks", "Apex Cloud acquisition talk, thoughts on APXC?", 8, 5),
    ("wallstreetbets", "APXC still cheap?", 30, 3),
    ("stocks", "Quantum Grid upgrade - is QGRD a buy here?", 6, 4),
    ("stocks", "QGRD insiders buying", 12, 2),
    ("stocks", "Long term QGRD thesis", 28, 5),
    ("mauerstrassenwetten", "Nordwind Energie hebt Prognose an – NWE Raketenstart?", 2, 6),
    ("mauerstrassenwetten", "Nordwind langweilig", 36, 1),
    ("mauerstrassenwetten", "Bavaria Robotics Gewinnwarnung, wer hält noch?", 5, 4),
    ("mauerstrassenwetten", "Daily Diskussionsthread", 20, 30),
    ("stocks", "Rate my portfolio", 40, 25),
    ("stocks", "Weekend discussion thread", 47, 3),
]


class DemoReddit:
    def __init__(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        self.posts: dict[str, list[dict]] = {}
        for sub, title, hours, n in REDDIT_POSTS:
            for k in range(n):
                created = (now - timedelta(hours=hours, minutes=k * 7)).timestamp()
                self.posts.setdefault(sub, []).append({"title": title, "selftext": "", "created_utc": created})
        for posts in self.posts.values():
            posts.sort(key=lambda p: -p["created_utc"])

    def listing(self, sub: str, after: str | None = None) -> dict:
        return {"data": {"children": [{"data": p} for p in self.posts.get(sub, [])], "after": None}}
