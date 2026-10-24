"""SEC EDGAR: Form-4-Insidergeschäfte und 8-K-Meldungen.

Nutzt ausschließlich die offiziellen, kostenlosen EDGAR-Endpunkte. Die SEC verlangt
einen User-Agent mit Name und E-Mail-Adresse und erlaubt höchstens 10 Anfragen pro
Sekunde (https://www.sec.gov/os/accessing-edgar-data).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from news_scanner.http import HttpClient
from news_scanner.models import InsiderTrade, NewsItem

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
CURRENT_FORM4_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&count={count}&output=atom"

ITEMS_8K = {
    "1.01": "Wesentlicher Vertrag abgeschlossen",
    "1.02": "Wesentlicher Vertrag beendet",
    "1.03": "Insolvenz",
    "2.01": "Übernahme/Verkauf von Vermögenswerten abgeschlossen",
    "2.02": "Quartals-/Jahreszahlen",
    "2.05": "Restrukturierung",
    "2.06": "Wertminderung",
    "3.01": "Delisting-Hinweis",
    "4.02": "Frühere Abschlüsse nicht mehr verlässlich",
    "5.01": "Kontrollwechsel",
    "5.02": "Wechsel im Management",
    "7.01": "Reg-FD-Mitteilung",
    "8.01": "Sonstige wesentliche Ereignisse",
}

PLACEHOLDER_UA = re.compile(r"example\.(com|org)|mustermann|your[-_ ]?mail", re.I)


def check_user_agent(user_agent: str) -> None:
    if not user_agent or "@" not in user_agent or PLACEHOLDER_UA.search(user_agent):
        raise ValueError(
            "SEC verlangt einen echten User-Agent mit Name und E-Mail "
            "(quellen.sec.user_agent oder Umgebungsvariable SEC_USER_AGENT)."
        )


class SecClient:
    def __init__(self, http: HttpClient, user_agent: str):
        self.http = http
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._ticker_map: dict[str, tuple[int, str]] | None = None

    def ticker_map(self) -> dict[str, tuple[int, str]]:
        if self._ticker_map is None:
            data = self.http.get_json(TICKERS_URL, cache_hours=24, headers=self.headers)
            self._ticker_map = {
                str(row["ticker"]).upper(): (int(row["cik_str"]), row["title"]) for row in data.values()
            }
        return self._ticker_map

    def submissions(self, cik: int) -> dict:
        return self.http.get_json(SUBMISSIONS_URL.format(cik=cik), headers=self.headers)

    def recent_filings(self, cik: int, forms: set[str], since: datetime) -> list[dict]:
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        out = []
        for i, form in enumerate(recent.get("form", [])):
            if form not in forms:
                continue
            filed = datetime.strptime(recent["filingDate"][i], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if filed < since:
                continue
            out.append(
                {
                    "form": form,
                    "accession": recent["accessionNumber"][i],
                    "filed": filed,
                    "doc": recent["primaryDocument"][i],
                    "items": (recent.get("items") or [""] * (i + 1))[i],
                    "accepted": (recent.get("acceptanceDateTime") or [""] * (i + 1))[i],
                }
            )
        return out

    def form4_xml(self, cik: int, accession: str, primary_doc: str) -> tuple[str, str]:
        acc = accession.replace("-", "")
        # primaryDocument zeigt oft auf die XSL-Darstellung ("xslF345X05/…xml"); die Roh-XML liegt daneben.
        doc = primary_doc.split("/")[-1]
        if not doc.endswith(".xml"):
            index = self.http.get_json(INDEX_URL.format(cik=cik, acc=acc), headers=self.headers)
            doc = next(f["name"] for f in index["directory"]["item"] if f["name"].endswith(".xml"))
        url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc)
        return self.http.get_text(url, headers=self.headers), url


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    if found is None:
        return ""
    value = found.find("value")
    return ((value.text if value is not None else found.text) or "").strip()


def _flag(node: ET.Element | None, path: str) -> bool:
    return _text(node, path).lower() in {"1", "true"}


def classify_role(title: str, is_director: bool, is_officer: bool, ten_pct: bool) -> tuple[str, bool]:
    t = title.lower()
    if re.search(r"\bceo\b|chief executive|vorstandsvorsitz|vorsitzende[rn]? des vorstands", t):
        return "CEO", True
    if re.search(r"\bcfo\b|chief financial|finanzvorstand|finanzchef", t):
        return "CFO", True
    if is_officer and title:
        return title, False
    if is_officer:
        return "Officer", False
    if is_director:
        return "Director", False
    if ten_pct:
        return "10%-Eigentümer", False
    return title or "Insider", False


def parse_form4(xml_text: str, link: str) -> list[InsiderTrade]:
    root = ET.fromstring(xml_text)
    ticker = _text(root, "issuer/issuerTradingSymbol").upper()
    company = _text(root, "issuer/issuerName")
    owners = root.findall("reportingOwner")
    names, roles = [], []
    for owner in owners:
        names.append(_text(owner, "reportingOwnerId/rptOwnerName"))
        rel = owner.find("reportingOwnerRelationship")
        roles.append(
            classify_role(
                _text(rel, "officerTitle"),
                _flag(rel, "isDirector"),
                _flag(rel, "isOfficer"),
                _flag(rel, "isTenPercentOwner"),
            )
        )
    role, is_top = next(((r, t) for r, t in roles if t), roles[0] if roles else ("Insider", False))
    trades = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        shares = float(_text(tx, "transactionAmounts/transactionShares") or 0)
        price = float(_text(tx, "transactionAmounts/transactionPricePerShare") or 0)
        date_raw = _text(tx, "transactionDate")
        date = datetime.strptime(date_raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc) if date_raw else None
        tx_type = {"P": "Kauf", "S": "Verkauf"}.get(code, "Sonstiges")
        trades.append(
            InsiderTrade(
                ticker=ticker,
                company=company,
                insider=" / ".join(n for n in names if n),
                role=role,
                is_ceo_cfo=is_top,
                tx_type=tx_type,
                tx_code=code,
                date=date,
                shares=shares,
                price=price,
                value=shares * price,
                currency="USD",
                link=link,
                source="SEC Form 4",
            )
        )
    return trades


def fetch_watchlist_filings(
    client: SecClient, tickers: list[str], days: int, max_form4_per_ticker: int = 15
) -> tuple[list[InsiderTrade], list[NewsItem], list[str]]:
    """Form 4 und 8-K der letzten ``days`` Tage für die Watchlist-Ticker."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    tmap = client.ticker_map()
    trades: list[InsiderTrade] = []
    news: list[NewsItem] = []
    errors: list[str] = []
    for ticker in tickers:
        if ticker not in tmap:
            errors.append(f"{ticker}: nicht in EDGAR-Tickerliste")
            continue
        cik, title = tmap[ticker]
        try:
            filings = client.recent_filings(cik, {"4", "8-K"}, since)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ticker}: {exc}")
            continue
        for f in [f for f in filings if f["form"] == "4"][:max_form4_per_ticker]:
            try:
                xml_text, url = client.form4_xml(cik, f["accession"], f["doc"])
                trades.extend(parse_form4(xml_text, url))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{ticker} Form 4 {f['accession']}: {exc}")
        for f in [f for f in filings if f["form"] == "8-K"]:
            news.append(_eight_k_item(ticker, title, cik, f))
    return trades, news, errors


def _eight_k_item(ticker: str, company: str, cik: int, f: dict) -> NewsItem:
    codes = [c.strip() for c in str(f.get("items", "")).split(",") if c.strip()]
    labels = [ITEMS_8K.get(c, f"Item {c}") for c in codes if c != "9.01"]
    published = f["filed"]
    if f.get("accepted"):
        try:
            published = datetime.fromisoformat(f["accepted"].replace("Z", "+00:00"))
        except ValueError:
            pass
    acc = f["accession"].replace("-", "")
    return NewsItem(
        title=f"{company} ({ticker}) 8-K: {', '.join(labels) or 'Unternehmensmeldung'}",
        link=ARCHIVE_URL.format(cik=cik, acc=acc, doc=f["doc"]),
        source="SEC 8-K",
        published=published,
        summary="8-K Items: " + ", ".join(codes),
        publisher="SEC EDGAR",
        tickers={ticker},
        kind="8-K",
    )


def fetch_current_form4(client: SecClient, count: int) -> tuple[list[InsiderTrade], list[str]]:
    """Marktweiter Scan: die neuesten Form-4-Einreichungen aus dem EDGAR-Atom-Feed."""
    import feedparser

    feed = feedparser.parse(client.http.get_text(CURRENT_FORM4_URL.format(count=count), headers=client.headers))
    trades: list[InsiderTrade] = []
    errors: list[str] = []
    seen: set[str] = set()
    for entry in feed.entries:
        m = re.search(r"/data/(\d+)/(\d{18})/", entry.get("link", ""))
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        cik, acc = int(m.group(1)), m.group(2)
        try:
            index = client.http.get_json(INDEX_URL.format(cik=cik, acc=acc), headers=client.headers)
            doc = next(f["name"] for f in index["directory"]["item"] if f["name"].endswith(".xml"))
            url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc)
            trades.extend(parse_form4(client.http.get_text(url, headers=client.headers), url))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Form 4 {acc}: {exc}")
    return trades, errors
