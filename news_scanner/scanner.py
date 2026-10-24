"""Ablaufsteuerung: Quellen abfragen, auswerten, Kandidaten bilden."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from news_scanner.analysis import insider as insider_mod
from news_scanner.analysis import scoring, technicals
from news_scanner.analysis.text import (
    Story,
    assign_tickers,
    catalyst_weight,
    cluster,
    detect_catalysts,
    rate_credibility,
)
from news_scanner.config import Position, WatchItem
from news_scanner.fmt import de
from news_scanner.models import Candidate, InsiderTrade, NewsItem, SourceStatus, Technicals
from news_scanner.sources import bafin, reddit, rss, sec

log = logging.getLogger(__name__)

NEGATIVE_FOR_LONG = {"Herabstufung", "Rechtliches/Untersuchung"}


def _summarize(errors: list[str], limit: int = 3) -> str:
    if not errors:
        return ""
    unique = list(dict.fromkeys(errors))
    more = f" (+{len(unique) - limit} weitere)" if len(unique) > limit else ""
    return "; ".join(e[:120] for e in unique[:limit]) + more


def _close_hour(ticker: str) -> float:
    """Handelsschluss in UTC-Stunden (Xetra 17:30 MEZ, US-Börsen 16:00 New York), grob."""
    return 15.5 if ticker.endswith((".DE", ".F", ".DU", ".MU", ".SG", ".HM", ".BE")) else 20.0


@dataclass
class PositionCheck:
    position: Position
    technicals: Technicals | None
    pnl_pct: float | None
    stories: list[Story]
    insider_sales: list[InsiderTrade]
    warnings: list[str]
    suggested_stop: float | None


@dataclass
class InsiderEntry:
    signal: insider_mod.InsiderSignal
    technicals: Technicals | None
    plan: object | None
    strength: int
    notes: list[str]


@dataclass
class ScanResult:
    generated: datetime
    candidates: list[Candidate] = field(default_factory=list)
    insiders: list[InsiderEntry] = field(default_factory=list)
    positions: list[PositionCheck] = field(default_factory=list)
    reddit: dict[str, dict] = field(default_factory=dict)
    statuses: list[SourceStatus] = field(default_factory=list)
    n_items: int = 0
    n_stories: int = 0
    demo: bool = False


class Scanner:
    def __init__(
        self,
        cfg: dict,
        positions: list[Position],
        http,
        market,
        data_dir: Path,
        reddit_factory: Callable | None = None,
        demo: bool = False,
    ):
        self.cfg = cfg
        self.positions = positions
        self.http = http
        self.market = market
        self.data_dir = data_dir
        self.reddit_factory = reddit_factory
        self.demo = demo
        self.result = ScanResult(generated=datetime.now(timezone.utc), demo=demo)
        self._prices: dict[str, tuple[pd.DataFrame, Technicals] | None] = {}
        self.watchlist: list[WatchItem] = list(cfg["watchlist"])
        known = {w.ticker for w in self.watchlist}
        for p in positions:
            if p.ticker not in known:
                self.watchlist.append(WatchItem(ticker=p.ticker, name=p.ticker, land="DE" if p.ticker.endswith(".DE") else "US",
                                                sec_ticker="" if "." in p.ticker else p.ticker, reddit_ticker=p.ticker.split(".")[0]))

    # ------------------------------------------------------------------ Hilfen

    def _source(self, name: str, fn: Callable[[], tuple[list, list[str]]]) -> list:
        """Führt eine Quelle aus; Ausnahmen und Teilfehler landen im Quellenstatus statt im Absturz."""
        try:
            items, errors = fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("Quelle %s fehlgeschlagen: %s", name, exc)
            self.result.statuses.append(SourceStatus(name, False, error=str(exc)[:200]))
            return []
        # Nur Fehler und gar nichts geliefert → Quelle gilt als ausgefallen.
        ok = not errors or bool(items)
        self.result.statuses.append(SourceStatus(name, ok, len(items), error=_summarize(errors)))
        return items

    def _q(self, key: str) -> dict:
        return self.cfg.get("quellen", {}).get(key, {}) or {}

    def prices(self, ticker: str) -> tuple[pd.DataFrame, Technicals] | None:
        if ticker not in self._prices:
            try:
                df, currency = self.market.history(ticker)
                self._prices[ticker] = (df, technicals.compute(df, currency))
            except Exception as exc:  # noqa: BLE001
                log.warning("Kursdaten %s: %s", ticker, exc)
                self.result.statuses.append(SourceStatus(f"Kurse {ticker}", False, error=str(exc)[:200]))
                self._prices[ticker] = None
        return self._prices[ticker]

    # ------------------------------------------------------------------ Quellen

    def collect_news(self) -> list[NewsItem]:
        hours = float(self.cfg.get("zeitraum", {}).get("news_stunden", 36))
        items: list[NewsItem] = []
        q = self._q("rss")
        if q.get("aktiv", True):
            got, st = rss.fetch_feeds(self.http, q.get("feeds", []), hours)
            items += got
            self.result.statuses += st
        q = self._q("google_news")
        if q.get("aktiv", True):
            got, st = rss.fetch_google_news(self.http, self.watchlist, q, hours)
            items += got
            self.result.statuses += st
        q = self._q("adhoc")
        if q.get("aktiv", True):
            got, st = rss.fetch_feeds(self.http, q.get("feeds", []), hours, kind="adhoc")
            items += got
            self.result.statuses += st
        return items

    def collect_sec(self) -> tuple[list[InsiderTrade], list[NewsItem]]:
        q = self._q("sec")
        if not q.get("aktiv", True):
            return [], []
        days = int(self.cfg.get("zeitraum", {}).get("insider_tage", 14))
        news_days = max(1, int(float(self.cfg.get("zeitraum", {}).get("news_stunden", 36)) // 24) + 1)
        client: sec.SecClient | None = None

        def make_client() -> sec.SecClient:
            if not self.demo:
                sec.check_user_agent(q.get("user_agent", ""))
            return sec.SecClient(self.http, q.get("user_agent", "") or "NewsScanner demo@localhost")

        trades: list[InsiderTrade] = []
        news: list[NewsItem] = []
        tickers = [w.sec_ticker for w in self.watchlist if w.sec_ticker]

        def watch() -> tuple[list, list[str]]:
            nonlocal client
            client = make_client()
            t, n, errors = sec.fetch_watchlist_filings(client, tickers, days, int(q.get("max_form4_pro_ticker", 15)))
            trades.extend(t)
            cutoff = self.result.generated.timestamp() - news_days * 86400
            news.extend(i for i in n if i.published and i.published.timestamp() >= cutoff)
            return t + n, errors

        self._source("SEC EDGAR (Watchlist: Form 4 + 8-K)", watch)
        if q.get("marktweit_form4", True):
            def market_wide() -> tuple[list, list[str]]:
                t, errors = sec.fetch_current_form4(client or make_client(), int(q.get("marktweit_anzahl", 40)))
                trades.extend(t)
                return t, errors

            self._source("SEC EDGAR (marktweit Form 4)", market_wide)
        # SEC-Ticker auf Watchlist-Ticker zurückführen
        sec_map = {w.sec_ticker: w.ticker for w in self.watchlist if w.sec_ticker}
        for t in trades:
            t.ticker = sec_map.get(t.ticker, t.ticker)
        unique = {(t.ticker, t.insider, t.date, t.shares, t.price, t.tx_code): t for t in trades}
        return list(unique.values()), news

    def collect_bafin(self) -> list[InsiderTrade]:
        q = self._q("bafin")
        if not q.get("aktiv", True):
            return []
        days = int(self.cfg.get("zeitraum", {}).get("insider_tage", 14))
        url = q.get("url", bafin.DEFAULT_URL)
        return self._source("BaFin Directors' Dealings", lambda: bafin.fetch_dealings(self.http, self.watchlist, days, url))

    def collect_reddit(self) -> dict[str, dict]:
        q = self._q("reddit")
        if not q.get("aktiv", True) or not self.reddit_factory:
            return {}
        counts: dict[str, dict] = {}

        def run() -> tuple[list, list[str]]:
            client = self.reddit_factory(q)
            posts, errors = [], []
            for sub in q.get("subreddits", []):
                try:
                    posts += reddit.fetch_posts(client.listing, sub, 48, int(q.get("max_seiten", 4)))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"r/{sub}: {exc}")
            if not posts:
                raise RuntimeError("; ".join(errors) or "keine Beiträge erhalten")
            counts.update(reddit.count_mentions(posts, self.watchlist, self.result.generated))
            reddit.merge_history(counts, self.data_dir / "reddit_verlauf.json",
                                 self.result.generated.strftime("%Y-%m-%d"))
            return posts, errors

        self._source("Reddit (" + ", ".join("r/" + s for s in q.get("subreddits", [])) + ")", run)
        return counts

    # ------------------------------------------------------------------ Auswertung

    def run(self) -> ScanResult:
        news = self.collect_news()
        trades, sec_news = self.collect_sec()
        trades += self.collect_bafin()
        self.result.reddit = self.collect_reddit()
        news += sec_news

        assign_tickers(news, self.watchlist)
        stories = cluster(news)
        for s in stories:
            rate_credibility(s)
            detect_catalysts(s)
        self.result.n_items, self.result.n_stories = len(news), len(stories)

        insider_cfg = self.cfg.get("insider", {})
        signals = {s.ticker: s for s in insider_mod.analyse_purchases(trades, insider_cfg)}
        self.result.candidates = self.build_candidates(stories, signals)
        self.result.insiders = self.build_insiders(signals)
        self.result.positions = self.check_positions(stories, insider_mod.sales(trades))
        return self.result

    def _buzz(self, ticker: str) -> tuple[bool, str]:
        c = self.result.reddit.get(ticker)
        if not c or c["heute"] == 0 and not c.get("vortag"):
            return False, ""
        min_mentions = int(self._q("reddit").get("min_erwaehnungen", 5))
        prev = c.get("vortag")
        text = f"Reddit: {c['heute']} Beiträge mit Erwähnung in 24 h (Vortag: {'–' if prev is None else prev})"
        buzz = c["heute"] >= min_mentions and (prev is None or c["heute"] >= max(prev * 1.5, prev + 3))
        return buzz, text

    def build_candidates(self, stories: list[Story], signals: dict) -> list[Candidate]:
        names = {w.ticker: w.name for w in self.watchlist}
        by_ticker: dict[str, list[Story]] = {}
        for s in stories:
            if s.catalysts:
                for t in s.tickers:
                    by_ticker.setdefault(t, []).append(s)

        candidates = []
        for ticker, tstories in by_ticker.items():
            tstories.sort(key=lambda s: (catalyst_weight(s) * s.credibility, len(s.items)), reverse=True)
            best = tstories[0]
            # Richtung aus den wichtigsten Storys des Werts gemeinsam, nicht nur aus einer
            net = sum(s.direction_score for s in tstories[:3])
            direction = "Long" if net >= 2 else "Short" if net <= -2 else "Neutral"
            sig = signals.get(ticker)
            if direction == "Neutral" and sig:
                direction = "Long"
            catalysts = list(dict.fromkeys(c for s in tstories[:3] for c in s.catalysts))
            if sig:
                catalysts.append("Insider-Käufe")
            price = self.prices(ticker)
            tech = price[1] if price else None
            tech_pts, tech_notes, react_atr, reaction, plan = None, [], None, "keine Kursdaten", None
            if price:
                df, tech = price
                pct, react_atr = technicals.reaction_since(df, best.first_seen, tech, _close_hour(ticker))
                reaction = scoring.reaction_text(pct, react_atr)
                tech_pts, tech_notes = technicals.confirmation(tech, direction)
                plan = scoring.trade_plan(tech, direction, react_atr, self.cfg.get("handelsplan", {}))
            buzz, buzz_text = self._buzz(ticker)
            n_sources = len({src for s in tstories[:1] for src in s.sources})
            strength, notes = scoring.signal_strength(
                catalyst_weight(best), best.credibility, n_sources, tech_pts,
                sig.score if sig else 0.0, buzz, react_atr, direction, best.rumor,
            )
            lines = []
            for s in tstories[:3]:
                label = " ⚠️ GERÜCHT" if s.rumor else ""
                lines.append(
                    f"{s.lead.title} — {', '.join(s.catalysts)}; Quellen: {', '.join(s.sources)}; "
                    f"Glaubwürdigkeit {de(s.credibility, 1)}/5{label}"
                )
            if sig:
                lines.append("Insider: " + "; ".join(sig.flags or ["Käufe gemeldet"]) +
                             f" (gesamt {de(sig.total_value, 0)} {sig.currency})")
            if buzz_text:
                lines.append(buzz_text)
            links = []
            for s in tstories[:3]:
                for i in s.items[:4]:
                    links.append((f"{i.publisher or i.source}: {i.title[:90]}", i.link))
            if sig:
                links += [(f"{t.source}: {t.insider}", t.link) for t in sig.trades[:2]]
            candidates.append(
                Candidate(
                    ticker=ticker, name=names.get(ticker, ticker), direction=direction, score=strength,
                    catalysts=catalysts, headline=best.lead.title, summary_lines=lines,
                    links=list(dict.fromkeys(links)), credibility=best.credibility, rumor=best.rumor,
                    reaction=reaction, technicals=tech, tech_notes=tech_notes, plan=plan, score_notes=notes,
                )
            )
        min_score = int(self.cfg.get("ausgabe", {}).get("min_signalstaerke", 1))
        limit = int(self.cfg.get("ausgabe", {}).get("max_kandidaten", 10))
        candidates = [c for c in candidates if c.score >= min_score]
        return sorted(candidates, key=lambda c: (-c.score, c.ticker))[:limit]

    def build_insiders(self, signals: dict) -> list[InsiderEntry]:
        out = []
        limit = int(self.cfg.get("ausgabe", {}).get("max_insider", 10))
        for sig in list(signals.values())[:limit]:
            price = self.prices(sig.ticker)
            tech = price[1] if price else None
            plan, tech_pts, notes = None, None, []
            if price:
                tech_pts, notes = technicals.confirmation(tech, "Long")
                pct, react = technicals.reaction_since(price[0], sig.last_date, tech, _close_hour(sig.ticker))
                notes.insert(0, "Kurs seit Insider-Kauf: " + scoring.reaction_text(pct, react).split(" (", 1)[1].rstrip(")"))
                plan = scoring.trade_plan(tech, "Long", react, self.cfg.get("handelsplan", {}))
            strength, _ = scoring.signal_strength(1.5, 5, 1, tech_pts, sig.score, False, None, "Long", False)
            out.append(InsiderEntry(sig, tech, plan, strength, notes))
        return sorted(out, key=lambda e: (-e.strength, -e.signal.total_value))

    def check_positions(self, stories: list[Story], sales: dict[str, list[InsiderTrade]]) -> list[PositionCheck]:
        checks = []
        for p in self.positions:
            price = self.prices(p.ticker)
            tech = price[1] if price else None
            pos_stories = [s for s in stories if p.ticker in s.tickers]
            pos_stories.sort(key=lambda s: (bool(s.catalysts), s.credibility), reverse=True)
            warnings = []
            pnl = suggested = None
            if tech:
                pnl = (tech.close / p.einstieg - 1) * 100
                suggested = tech.close - 1.5 * tech.atr
                if p.stop is not None and tech.close <= p.stop:
                    warnings.append(f"Kurs {de(tech.close)} hat den Stop {de(p.stop)} erreicht/unterschritten")
                elif p.stop is not None and tech.close - p.stop < 0.5 * tech.atr:
                    warnings.append(f"Kurs nur {de((tech.close - p.stop) / tech.atr, 1)} ATR über dem Stop")
                if tech.close < tech.sma50:
                    warnings.append("Schlusskurs unter SMA50 – Trend geschwächt")
            for s in pos_stories:
                if s.direction == "Short" or set(s.catalysts) & NEGATIVE_FOR_LONG:
                    warnings.append(f"Negative Meldung: {s.lead.title}")
            if sales.get(p.ticker):
                total = sum(t.value for t in sales[p.ticker])
                warnings.append(f"{len(sales[p.ticker])} Insider-Verkäufe, gesamt {de(total, 0)} {sales[p.ticker][0].currency}")
            checks.append(PositionCheck(p, tech, pnl, pos_stories[:5], sales.get(p.ticker, []), warnings, suggested))
        return checks
