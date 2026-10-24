"""Textanalyse: Ticker zuordnen, Dubletten zusammenführen, Glaubwürdigkeit, Katalysatoren."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

from news_scanner.config import WatchItem
from news_scanner.models import NewsItem

# ---------------------------------------------------------------- Ticker-Zuordnung


def assign_tickers(items: list[NewsItem], watchlist: list[WatchItem]) -> None:
    patterns = []
    for w in watchlist:
        sym = re.escape(w.ticker.split(".")[0])
        parts = [rf"\${sym}\b", rf"\((?:NASDAQ:|NYSE:|XETRA:)?\s*{sym}\)"]
        parts += [rf"\b{re.escape(t)}\b" for t in w.search_terms]
        patterns.append((w.ticker, re.compile("|".join(parts), re.I)))
    for item in items:
        text = f"{item.title} {item.summary}"
        for ticker, pat in patterns:
            if pat.search(text):
                item.tickers.add(ticker)


# ---------------------------------------------------------------- Dubletten


_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "at", "is", "its", "with",
         "der", "die", "das", "und", "im", "in", "mit", "für", "auf", "von", "zu", "den", "dem", "ein", "eine"}


def _normalize(title: str) -> str:
    title = unicodedata.normalize("NFKD", title.lower())
    title = re.sub(r"[^\w\s%$.]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def _tokens(title: str) -> set[str]:
    return {t for t in _normalize(title).split() if t not in _STOP and len(t) > 1}


def similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    jaccard = len(ta & tb) / len(ta | tb)
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return jaccard >= 0.5 or overlap >= 0.75 or SequenceMatcher(None, _normalize(a), _normalize(b)).ratio() >= 0.8


@dataclass
class Story:
    items: list[NewsItem]
    tickers: set[str] = field(default_factory=set)
    catalysts: list[str] = field(default_factory=list)
    direction: str = "Neutral"
    direction_score: int = 0
    credibility: float = 0.0
    rumor: bool = False

    @property
    def lead(self) -> NewsItem:
        return max(self.items, key=lambda i: (source_tier(i), -len(i.title)))

    @property
    def first_seen(self) -> datetime | None:
        dates = [i.published for i in self.items if i.published]
        return min(dates) if dates else None

    @property
    def sources(self) -> list[str]:
        return list(dict.fromkeys(i.publisher or i.source for i in self.items))


def cluster(items: list[NewsItem]) -> list[Story]:
    """Fasst gleiche Meldungen aus mehreren Quellen zu einer Story zusammen."""
    stories: list[Story] = []
    seen_links: set[str] = set()
    for item in sorted(items, key=lambda i: i.published.timestamp() if i.published else 0):
        if item.link and item.link in seen_links:
            continue
        seen_links.add(item.link)
        for story in stories:
            shares_ticker = not item.tickers or not story.tickers or item.tickers & story.tickers
            if shares_ticker and any(similar(item.title, other.title) for other in story.items):
                story.items.append(item)
                story.tickers |= item.tickers
                break
        else:
            stories.append(Story(items=[item], tickers=set(item.tickers)))
    return stories


# ---------------------------------------------------------------- Glaubwürdigkeit

TIERS = {
    5: ("sec", "bafin", "eqs", "dgap", "reuters", "bloomberg", "associated press", "dow jones", "business wire",
        "globenewswire", "pr newswire", "ad-hoc"),
    4: ("cnbc", "marketwatch", "wall street journal", "wsj", "financial times", "handelsblatt", "barron",
        "faz", "börsen-zeitung", "boersen-zeitung", "nikkei"),
    3: ("yahoo", "investing.com", "finanzen.net", "seeking alpha", "benzinga", "motley fool", "der aktionär",
        "onvista", "ariva", "zacks", "tipranks", "google news"),
}

RUMOR_RE = re.compile(
    r"reportedly|people familiar|sources? (say|said)|according to (people|sources)|rumou?r|speculat|"
    r"is said to|considering|weighs?|exploring|in talks|could (be|buy|sell)|may (be|buy|sell)|unconfirmed|"
    r"gerücht|insidern zufolge|kreisen zufolge|in kreisen|angeblich|spekulation|erwäg|soll (angeblich )?|"
    r"laut (einem )?bericht|medienbericht",
    re.I,
)
OFFICIAL_KINDS = {"8-K", "adhoc"}


def source_tier(item: NewsItem) -> int:
    if item.kind in OFFICIAL_KINDS:
        return 5
    name = f"{item.publisher} {item.source}".lower()
    for tier, keys in TIERS.items():
        if any(k in name for k in keys):
            return tier
    return 2


def rate_credibility(story: Story) -> None:
    best = max(source_tier(i) for i in story.items)
    distinct = len(set(story.sources))
    official = any(i.kind in OFFICIAL_KINDS for i in story.items)
    rumor = not official and any(RUMOR_RE.search(f"{i.title} {i.summary}") for i in story.items)
    score = best + min(distinct - 1, 2) * 0.5 - (1.5 if rumor else 0)
    story.credibility = round(max(1.0, min(score, 5.0)), 1)
    story.rumor = rumor


# ---------------------------------------------------------------- Katalysatoren

CATALYSTS: dict[str, tuple[float, re.Pattern]] = {
    "Übernahme/Fusion": (3.0, re.compile(
        r"acquir|merger|takeover|buyout|tender offer|to buy .* for \$|deal to buy|übernahme|übernimmt|fusion|"
        r"kaufangebot|übernahmeangebot|8-K:.*(Übernahme|Kontrollwechsel)", re.I)),
    "Prognoseänderung": (2.5, re.compile(
        r"raises? (its |full[- ]year )?(guidance|outlook|forecast)|(cuts?|lowers?) (its |full[- ]year )?(guidance|outlook|forecast)|"
        r"profit warning|prognose (an|erhöht|angehoben|gesenkt)|(hebt|erhöht|senkt|kappt) .{0,40}prognose|"
        r"gewinnwarnung|umsatzwarnung", re.I)),
    "Zulassung": (3.0, re.compile(
        r"\bfda\b|approv|clearance|cleared|\bema\b|chmp|breakthrough designation|zulassung|zugelassen|genehmig", re.I)),
    "Quartalszahlen": (2.5, re.compile(
        r"earnings|quarterly|results|\bq[1-4]\b|revenue|eps\b|profit|guidance|outlook|forecast|"
        r"quartal|zahlen|umsatz|gewinn|prognose|ausblick|ergebnis|8-K:.*Quartals", re.I)),
    "Hochstufung": (2.0, re.compile(
        r"upgrade|raises? (its )?(price )?target|price target (raised|hike)|to (buy|outperform|overweight)|"
        r"initiat\w+ .* (buy|outperform|overweight)|hochgestuft|hochstufung|stuft .{0,40}\bhoch\b|kursziel (angehoben|erhöht)|"
        r"auf (kaufen|buy|outperform|overweight)", re.I)),
    "Herabstufung": (2.0, re.compile(
        r"downgrade|cuts? (its )?(price )?target|price target (cut|lowered)|to (sell|underperform|underweight|neutral)|"
        r"herabgestuft|abgestuft|abstufung|stuft .{0,40}\bab\b|kursziel (gesenkt|reduziert)|auf (verkaufen|sell|underweight)", re.I)),
    "Management-Wechsel": (1.0, re.compile(
        r"\bceo\b.*(steps down|resign|depart|appoint|named)|chief executive|vorstandschef|rücktritt|"
        r"8-K:.*Management", re.I)),
    "Großauftrag/Partnerschaft": (1.5, re.compile(
        r"contract|partnership|collaborat|agreement|auftrag|partnerschaft|kooperation|8-K:.*Vertrag", re.I)),
    "Rechtliches/Untersuchung": (1.5, re.compile(
        r"lawsuit|investigation|probe|sec charges|recall|antitrust|klage|ermittlung|rückruf|kartell", re.I)),
    "Kapitalmaßnahme": (1.0, re.compile(
        r"buyback|share repurchase|offering|dividend|rückkauf|kapitalerhöhung|dividende", re.I)),
}

POSITIVE = re.compile(
    r"beat|beats|tops|surpass|exceed|raises|raised|record|surge|soar|jump|rall|climb|upgrade|approv|"
    r"strong|outperform|wins?\b|buyback|übertrifft|übertroffen|hebt .* an|angehoben|erhöht|rekord|steigt|springt|"
    r"klettert|hochgestuft|zugelassen|stark|gewinnt",
    re.I,
)
NEGATIVE = re.compile(
    r"miss|misses|cuts?\b|lower|weak|plunge|tumble|slump|fall|drop|sink|downgrade|reject|recall|probe|"
    r"lawsuit|warn|delay|halt|layoff|verfehlt|senkt|gesenkt|schwach|bricht ein|fällt|sinkt|stürzt|abgestuft|"
    r"warnung|gewinnwarnung|verzöger|abgelehnt|klage|stellenabbau",
    re.I,
)


def detect_catalysts(story: Story) -> None:
    text = " ".join(f"{i.title} {i.summary}" for i in story.items)
    titles = " ".join(i.title for i in story.items)
    found = [name for name, (_, pat) in CATALYSTS.items() if pat.search(titles)]
    if not found:
        found = [name for name, (_, pat) in CATALYSTS.items() if pat.search(text)]
    story.catalysts = sorted(found, key=lambda n: -CATALYSTS[n][0])
    pos = len(POSITIVE.findall(titles)) * 2 + len(POSITIVE.findall(text))
    neg = len(NEGATIVE.findall(titles)) * 2 + len(NEGATIVE.findall(text))
    if "Hochstufung" in found:
        pos += 3
    if "Herabstufung" in found:
        neg += 3
    if "Übernahme/Fusion" in found and re.search(r"(to be|being) acquired|übernommen werden|takeover (bid|offer) for", text, re.I):
        pos += 3
    story.direction_score = pos - neg
    story.direction = "Long" if pos - neg >= 2 else "Short" if neg - pos >= 2 else "Neutral"


def catalyst_weight(story: Story) -> float:
    return max((CATALYSTS[c][0] for c in story.catalysts), default=0.0)
