"""Markdown-Bericht."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_scanner.fmt import de, sde
from news_scanner.models import Candidate, TradePlan
from news_scanner.scanner import ScanResult

TZ = ZoneInfo("Europe/Berlin")
ARROW = {"Long": "🟢 Long", "Short": "🔴 Short", "Neutral": "⚪ Neutral"}


def _date(d: datetime | None) -> str:
    return d.astimezone(TZ).strftime("%d.%m.%Y") if d else "–"


def _bar(score: int) -> str:
    return "█" * score + "░" * (10 - score)


def _md_link(title: str, url: str) -> str:
    title = title.replace("[", "(").replace("]", ")").replace("|", "/")
    return f"[{title}]({url})" if url else title


def _plan(plan: TradePlan | None, cur: str) -> list[str]:
    if not plan:
        return ["- **Handelsplan:** keiner (Richtung unklar oder keine Kursdaten)"]
    lines = [
        "| Einstiegsbereich | Stop-Loss (1,5 × ATR) | Kursziel 1 | Kursziel 2 |",
        "|---|---|---|---|",
        f"| {de(plan.entry_low)} – {de(plan.entry_high)} {cur} | {de(plan.stop)} | {de(plan.target1)} | {de(plan.target2)} |",
        "",
        "**Ausstiegsregeln:**",
    ]
    lines += [f"{i}. {r}" for i, r in enumerate(plan.exit_rules, 1)]
    return lines


def _candidate(n: int, c: Candidate) -> list[str]:
    t = c.technicals
    cur = t.currency if t else ""
    out = [
        f"### {n}. {c.ticker} – {c.name}  ·  {ARROW[c.direction]}  ·  Signalstärke **{c.score}/10**",
        "",
        f"`{_bar(c.score)}`" + ("  ⚠️ **Gerücht – unbestätigt**" if c.rumor else ""),
        "",
        f"- **Katalysator:** {', '.join(c.catalysts)}",
        f"- **Glaubwürdigkeit:** {de(c.credibility, 1)}/5",
        f"- **Kursreaktion:** {c.reaction}",
    ]
    if t:
        out.append(
            f"- **Kurs:** {de(t.close)} {cur} ({sde(t.change_pct)} % ggü. Vortag) · RSI {t.rsi:.0f} · "
            f"SMA20 {de(t.sma20)} · SMA50 {de(t.sma50)} · ATR {de(t.atr)} · Volumen {de(t.volume_ratio, 1)}× Ø"
        )
    out += ["", "**Was ist passiert?**"]
    out += [f"- {line}" for line in c.summary_lines]
    if c.tech_notes:
        out += ["", "**Technische Bestätigung:**"]
        out += [f"- {line}" for line in c.tech_notes]
    out += [""] + _plan(c.plan, cur)
    out += ["", "<details><summary>Quellen & Bewertung</summary>", ""]
    out += [f"- {_md_link(title, url)}" for title, url in c.links]
    out += ["", "Signalstärke: " + " · ".join(c.score_notes), "", "</details>", ""]
    return out


def render(res: ScanResult) -> str:
    now = res.generated.astimezone(TZ)
    out = [f"# News-Scanner – Bericht vom {now:%d.%m.%Y, %H:%M} Uhr", ""]
    if res.demo:
        out += [
            "> **DEMO-BERICHT:** Erzeugt mit `--demo` aus den mitgelieferten Beispieldaten "
            "(`news_scanner/demo/`). Meldungen, Insider-Geschäfte, Reddit-Zahlen und Kurse sind "
            "**erfunden** und dienen nur dazu, Ablauf und Format zu zeigen.",
            "",
        ]
    ok = sum(1 for s in res.statuses if s.ok)
    out += [
        f"{res.n_items} Meldungen → {res.n_stories} Storys nach Zusammenführung · "
        f"{len(res.candidates)} News-Kandidaten · {len(res.insiders)} Insider-Signale · "
        f"Quellen: {ok}/{len(res.statuses)} erreichbar",
        "",
        "## Top-News-Kandidaten",
        "",
    ]
    if res.candidates:
        out += ["| # | Ticker | Richtung | Stärke | Katalysator | Reaktion |", "|---|---|---|---|---|---|"]
        for i, c in enumerate(res.candidates, 1):
            out.append(f"| {i} | **{c.ticker}** | {ARROW[c.direction]} | {c.score}/10 | "
                       f"{', '.join(c.catalysts[:2])} | {c.reaction.split(' (')[0]} |")
        out.append("")
        for i, c in enumerate(res.candidates, 1):
            out += _candidate(i, c)
    else:
        out += ["_Keine Kandidaten mit erkennbarem Katalysator gefunden._", ""]

    out += ["## Auffällige Insider-Käufe", ""]
    if res.insiders:
        out += ["| Ticker | Unternehmen | Käufer | Summe | Merkmale | Stärke |", "|---|---|---|---|---|---|"]
        for e in res.insiders:
            s = e.signal
            out.append(f"| **{s.ticker}** | {s.company} | {len(s.buyers)} | {de(s.total_value, 0)} {s.currency} | "
                       f"{'; '.join(s.flags) or '–'} | {e.strength}/10 |")
        out.append("")
        for e in res.insiders:
            s = e.signal
            cur = e.technicals.currency if e.technicals else s.currency
            out += [f"### {s.ticker} – {s.company}  ·  🟢 Long  ·  Signalstärke **{e.strength}/10**", ""]
            out += ["| Datum | Insider | Funktion | Stück | Preis | Betrag | Quelle |", "|---|---|---|---|---|---|---|"]
            for t in s.trades:
                out.append(f"| {_date(t.date)} | {t.insider} | {t.role}{' ⭐' if t.is_ceo_cfo else ''} | "
                           f"{de(t.shares, 0)} | {de(t.price)} | {de(t.value, 0)} {t.currency} | "
                           f"{_md_link(t.source, t.link)} |")
            out.append("")
            if e.notes:
                out += [f"- {n}" for n in e.notes] + [""]
            out += _plan(e.plan, cur) + [""]
    else:
        out += ["_Keine auffälligen Insider-Käufe im Zeitraum._", ""]

    out += ["## Offene Positionen (positionen.yaml)", ""]
    if res.positions:
        out += ["| Ticker | Einstieg | Kurs | G/V | Stop | Vorschlag Stop (1,5 × ATR) | Status |", "|---|---|---|---|---|---|---|"]
        for pc in res.positions:
            p, t = pc.position, pc.technicals
            status = "⚠️ " + str(len(pc.warnings)) + " Hinweis(e)" if pc.warnings else "✅ unauffällig"
            out.append(f"| **{p.ticker}** | {de(p.einstieg)} | {de(t.close) if t else '–'} | "
                       f"{'–' if pc.pnl_pct is None else sde(pc.pnl_pct) + ' %'} | {de(p.stop)} | "
                       f"{de(pc.suggested_stop)} | {status} |")
        out.append("")
        for pc in res.positions:
            out.append(f"**{pc.position.ticker}**" + (f" – {pc.position.notiz}" if pc.position.notiz else ""))
            out += [f"- ⚠️ {w}" for w in pc.warnings]
            for s in pc.stories:
                cats = f" ({', '.join(s.catalysts)})" if s.catalysts else ""
                out.append(f"- 📰 {_md_link(s.lead.title, s.lead.link)}{cats} – {s.direction}, "
                           f"Glaubwürdigkeit {de(s.credibility, 1)}/5{' ⚠️ Gerücht' if s.rumor else ''}")
            for t in pc.insider_sales[:5]:
                out.append(f"- 🔻 Insider-Verkauf {_date(t.date)}: {t.insider} ({t.role}) "
                           f"{de(t.value, 0)} {t.currency} – {_md_link(t.source, t.link)}")
            if not pc.warnings and not pc.stories:
                out.append("- Keine neuen Meldungen oder Insider-Verkäufe.")
            out.append("")
    else:
        out += ["_Keine offenen Positionen eingetragen._", ""]

    if res.reddit:
        out += ["## Reddit-Stimmung (Erwähnungen in Beiträgen, 24 h)", "",
                "| Ticker | Heute | Vortag | Veränderung |", "|---|---|---|---|"]
        for ticker, c in sorted(res.reddit.items(), key=lambda kv: -kv[1]["heute"]):
            if c["heute"] == 0 and not c.get("vortag"):
                continue
            ch = c.get("veraenderung")
            out.append(f"| {ticker} | {c['heute']} | {'–' if c.get('vortag') is None else c['vortag']} | "
                       f"{'–' if ch is None else f'{ch:+d}'} |")
        out.append("")

    out += ["## Quellenstatus", "", "| Quelle | Status | Einträge | Hinweis |", "|---|---|---|---|"]
    for s in res.statuses:
        out.append(f"| {s.name} | {'✅' if s.ok else '❌'} | {s.items} | {s.error.replace('|', '/') or ''} |")
    out += [
        "",
        "---",
        "_Automatisch erzeugt. Keine Anlageberatung – alle Angaben ohne Gewähr. Signale vor jeder "
        "Order selbst prüfen._",
        "",
    ]
    return "\n".join(out)


def write(res: ScanResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = res.generated.astimezone(TZ).strftime("%Y-%m-%d_%H%M")
    path = out_dir / f"{'demo_' if res.demo else ''}news-scan_{stamp}.md"
    path.write_text(render(res), encoding="utf-8")
    return path
