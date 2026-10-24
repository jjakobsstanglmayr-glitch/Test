"""Auswertung von Insidergeschäften: CEO/CFO-Käufe, Cluster, Betrag im Verhältnis zum Gehalt."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from news_scanner.fmt import de
from news_scanner.models import InsiderTrade


@dataclass
class InsiderSignal:
    ticker: str
    company: str
    trades: list[InsiderTrade]
    total_value: float
    currency: str
    buyers: list[str]
    flags: list[str] = field(default_factory=list)
    score: float = 0.0  # 0–4 Punkte, fließt in die Signalstärke ein

    @property
    def last_date(self):
        dates = [t.date for t in self.trades if t.date]
        return max(dates) if dates else None


def _salary(trade: InsiderTrade, cfg: dict) -> float:
    per_ticker = cfg.get("gehalt_pro_ticker", {}).get(trade.ticker, {})
    table = {**cfg.get("gehalt_schaetzung", {}), **per_ticker}
    key = trade.role if trade.role in table else ("Director" if "director" in trade.role.lower() else "default")
    return float(table.get(key, table.get("default", 500_000)))


def analyse_purchases(trades: list[InsiderTrade], cfg: dict) -> list[InsiderSignal]:
    """Bewertet Käufe am offenen Markt (SEC-Code P bzw. BaFin "Kauf") je Unternehmen."""
    min_value = float(cfg.get("mindestbetrag", 25_000))
    cluster_days = int(cfg.get("cluster_tage", 10))
    cluster_min = int(cfg.get("cluster_mindestanzahl", 2))
    ratio_flag = float(cfg.get("gehalt_verhaeltnis_auffaellig", 0.5))

    by_ticker: dict[str, list[InsiderTrade]] = defaultdict(list)
    for t in trades:
        if t.tx_type == "Kauf" and t.value > 0:
            by_ticker[t.ticker].append(t)

    signals = []
    for ticker, buys in by_ticker.items():
        total = sum(t.value for t in buys)
        if total < min_value:
            continue
        per_person: dict[str, float] = defaultdict(float)
        for t in buys:
            per_person[t.insider] += t.value
        sig = InsiderSignal(
            ticker=ticker,
            company=buys[0].company,
            trades=sorted(buys, key=lambda t: t.date.timestamp() if t.date else 0, reverse=True),
            total_value=total,
            currency=buys[0].currency,
            buyers=list(per_person),
        )
        top = [t for t in buys if t.is_ceo_cfo]
        if top:
            roles = sorted({t.role for t in top})
            sig.flags.append(f"Kauf durch {'/'.join(roles)}")
            sig.score += 1.5
        dated = sorted(t.date for t in buys if t.date)
        if len(per_person) >= cluster_min and dated and dated[-1] - dated[0] <= timedelta(days=cluster_days):
            sig.flags.append(f"Cluster: {len(per_person)} Insider kaufen innerhalb von {cluster_days} Tagen")
            sig.score += 1.5
        best_ratio, best_name = 0.0, ""
        for name, value in per_person.items():
            trade = next(t for t in buys if t.insider == name)
            ratio = value / _salary(trade, cfg)
            if ratio > best_ratio:
                best_ratio, best_name = ratio, name
        if best_ratio >= ratio_flag:
            sig.flags.append(f"Großer Betrag: {best_name} ≈ {de(best_ratio, 1)}× geschätztes Jahresgehalt")
            sig.score += 1.0 if best_ratio < 2 else 1.5
        if not sig.flags and total >= float(cfg.get("grosser_betrag", 250_000)):
            sig.flags.append("Hoher Gesamtbetrag")
            sig.score += 0.5
        sig.score = min(sig.score, 4.0)
        signals.append(sig)
    return sorted(signals, key=lambda s: (-s.score, -s.total_value))


def sales(trades: list[InsiderTrade]) -> dict[str, list[InsiderTrade]]:
    out: dict[str, list[InsiderTrade]] = defaultdict(list)
    for t in trades:
        if t.tx_type == "Verkauf":
            out[t.ticker].append(t)
    return out
