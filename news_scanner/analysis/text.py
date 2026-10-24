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
    """Ordnet Meldungen über Cashtag, Börsenkürzel in Klammern oder Firmennamen zu.

    Begriffe aus ``ausschluss`` (z. B. "Siemens Energy" für Siemens) werden vorher entfernt.
    """
    patterns = []
    for w in watchlist:
        sym = re.escape(w.ticker.split(".")[0])
        parts = [rf"\${sym}\b", rf"\((?:NASDAQ:|NYSE:|XETRA:|ETR:)?\s*{sym}\)"]
        parts += [rf"\b{re.escape(t)}\b" for t in w.search_terms]
        excl = [rf"(?i:\b{re.escape(x)}\b)" for x in w.exclude]
        # Name gefolgt von einem fremden Kürzel, z. B. "Apple Hospitality REIT (APLE)"
        excl += [rf"\b{re.escape(t)}\b[^()]{{0,40}}\((?!{sym}\))(?:[A-Z]+:\s*)?[A-Z][A-Z.]{{0,6}}\)"
                 for t in w.search_terms]
        exclude = re.compile("|".join(excl))
        patterns.append((w.ticker, re.compile("|".join(parts), re.I), exclude))
    for item in items:
        text = f"{item.title} {item.summary}"
        for ticker, pat, exclude in patterns:
            if pat.search(exclude.sub(" ", text)):
                item.tickers.add(ticker)


# ---------------------------------------------------------------- Dubletten


_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "at", "is", "its", "with",
         "der", "die", "das", "und", "im", "in", "mit", "für", "auf", "von", "zu", "den", "dem", "ein", "eine",
         # Allerweltswörter in Finanz-Schlagzeilen, die allein keine gleiche Meldung belegen
         "stock", "stocks", "shares", "share", "aktie", "aktien", "inc", "corp", "ag", "se", "co"}


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
    shared = len(ta & tb)
    jaccard = shared / len(ta | tb)
    overlap = shared / min(len(ta), len(tb))
    return (
        (jaccard >= 0.5 and shared >= 3)
        or (overlap >= 0.8 and shared >= 4)
        or SequenceMatcher(None, _normalize(a), _normalize(b)).ratio() >= 0.85
    )


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
        "globenewswire", "pr newswire"),
    4: ("cnbc", "marketwatch", "wall street journal", "wsj", "financial times", "handelsblatt", "barron",
        "faz", "börsen-zeitung", "boersen-zeitung", "nikkei"),
    3: ("yahoo", "investing.com", "finanzen.net", "seeking alpha", "benzinga", "motley fool", "der aktionär",
        "onvista", "ariva", "zacks", "tipranks", "barron", "investor's business daily", "welt",
        "spiegel", "tagesschau", "n-tv", "manager magazin", "wirtschaftswoche", "ad-hoc-news"),
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
    # Bei Google News zählt nur der eigentliche Herausgeber, nicht Google als Vermittler.
    name = (item.publisher if item.source.startswith("Google News") else f"{item.publisher} {item.source}").lower()
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
        r"\bto acquire\b|acquisition of|agrees? to (buy|acquire)|merger|takeover|buyout|tender offer|deal to buy|"
        r"to be acquired|übernahme|übernimmt|fusion|kaufangebot|übernahmeangebot|8-K:.*(Übernahme|Kontrollwechsel)", re.I)),
    "Zulassung": (3.0, re.compile(
        r"\bfda\b.*(approv|clear|reject)|approv\w* by (the )?(fda|ema)|\bema\b|chmp|breakthrough designation|"
        r"zulassung|zugelassen", re.I)),
    "Prognoseänderung": (2.5, re.compile(
        r"raises? (its |full[- ]year |annual )?(guidance|outlook|forecast)|(cuts?|lowers?|slashes) (its |full[- ]year |annual )?"
        r"(guidance|outlook|forecast)|profit warning|prognose (an|erhöht|angehoben|gesenkt|bestätigt)|"
        r"(hebt|erhöht|senkt|kappt|bestätigt) .{0,40}prognose|gewinnwarnung|umsatzwarnung", re.I)),
    "Quartalszahlen": (2.5, re.compile(
        r"(reports?|reported|posts?|delivers?) .{0,40}(results|earnings|quarter|revenue|profit)|"
        r"(beats?|misses?|tops?|surpass\w*|exceeds?) .{0,30}(estimates|expectations|forecasts?)|"
        r"(q[1-4]|quarterly|third[- ]quarter|second[- ]quarter|first[- ]quarter|fourth[- ]quarter) (results|earnings|revenue|profit)|"
        r"earnings (beat|miss)|quartalszahlen|legt .{0,20}zahlen vor|nach (den )?zahlen|meldet .{0,30}quartal|"
        r"(übertrifft|verfehlt) .{0,30}erwartungen|8-K:.*Quartals", re.I)),
    "Hochstufung": (2.0, re.compile(
        r"upgrad\w*|raises? (its )?price target|price target (raised|hike|boost)|"
        r"initiat\w+ .{0,40}(buy|outperform|overweight)|hochgestuft|hochstufung|stuft .{0,40}\bhoch\b|"
        r"kursziel (angehoben|erhöht)|hebt .{0,30}kursziel", re.I)),
    "Herabstufung": (2.0, re.compile(
        r"downgrad\w*|cuts? (its )?price target|price target (cut|lowered)|"
        r"herabgestuft|abgestuft|abstufung|stuft .{0,40}\bab\b|kursziel (gesenkt|reduziert)|senkt .{0,30}kursziel", re.I)),
    "Management-Wechsel": (1.0, re.compile(
        r"\b(ceo|cfo)\b.{0,30}(steps down|resign|depart|to leave|appoint|named)|"
        r"(vorstandschef|finanzchef|ceo|cfo) .{0,30}(rücktritt|tritt zurück|verlässt|abberufen)|8-K:.*Management", re.I)),
    "Großauftrag/Partnerschaft": (1.5, re.compile(
        r"(wins?|awarded|secures?|signs?) .{0,40}(contract|deal|order)|partnership with|"
        r"großauftrag|auftrag (über|im wert|von)|erhält .{0,30}auftrag|partnerschaft mit|8-K:.*Vertrag", re.I)),
    "Rechtliches/Untersuchung": (1.5, re.compile(
        r"lawsuit|investigation|probe|sec charges|recall|antitrust|klage|ermittlung|rückruf|kartellverfahren", re.I)),
    "Kapitalmaßnahme": (1.0, re.compile(
        r"buyback|share repurchase|(raises?|hikes?|cuts?|suspends?) .{0,20}dividend|special dividend|"
        r"aktienrückkauf|kapitalerhöhung|sonderdividende|dividende (erhöht|gesenkt|gestrichen)|"
        r"(erhöht|senkt|streicht) .{0,20}dividende", re.I)),
}

# Ratgeber, Rückblicke, Vorschauen und 13F-Meldungen von Fonds sind keine Katalysatoren.
NOISE = re.compile(
    r"stocks? to (buy|sell|watch)|should you|is it (time|too late)|here'?s (why|what|how)|what to (expect|watch)|"
    r"need to know|days away|ahead of (its |the )?(earnings|results)|earnings preview|"
    r"(shares|stock|stake|position)( in)?.{0,40}(acquired|sold|bought|purchased|raised|lowered|trimmed) by|"
    r"(acquires?|sells?|buys?) (new )?(shares|stake|position)|holdings? in|"
    r"better .{0,20}stock|millionaire|(buy|hold) (and hold|forever)|take profits|"
    r"so viel hätte|hätte .{0,30}(investition|gebracht)|lohnt sich|vor (den )?zahlen|was anleger wissen",
    re.I,
)

POSITIVE = re.compile(
    r"beat|beats|tops|surpass|exceed|raises|raised|record|surge|soar|jump|rall|climb|upgrade|approv|"
    r"strong|outperform|wins?\b|buyback|übertrifft|übertroffen|hebt .* an|angehoben|erhöht|rekord|steigt|springt|"
    r"klettert|hochgestuft|zugelassen|stark|gewinnt|kursgewinn|legt zu|im plus|fester|hebt .{0,30}an",
    re.I,
)
NEGATIVE = re.compile(
    r"miss|misses|cuts?\b|lower|weak|plunge|tumble|slump|fall|drop|sink|downgrade|reject|recall|probe|"
    r"lawsuit|warn|delay|halt|layoff|verfehlt|senkt|gesenkt|schwach|bricht ein|fällt|sinkt|stürzt|abgestuft|"
    r"warnung|gewinnwarnung|verzöger|abgelehnt|klage|stellenabbau|kursverlust|verliert|im minus|gibt nach|rutscht",
    re.I,
)


def detect_catalysts(story: Story) -> None:
    relevant = [i for i in story.items if not NOISE.search(i.title)]
    if not relevant:
        story.catalysts, story.direction, story.direction_score = [], "Neutral", 0
        return
    text = " ".join(f"{i.title} {i.summary}" for i in relevant)
    titles = " ".join(i.title for i in relevant)
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
