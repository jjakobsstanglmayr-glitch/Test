"""Signalstärke (1–10) und Handelsplan (Einstieg, Stop 1,5 × ATR, Ziele, Ausstiegsregeln)."""

from __future__ import annotations

from news_scanner.fmt import de, sde
from news_scanner.models import Technicals, TradePlan


def signal_strength(
    catalyst_weight: float,
    credibility: float,
    n_sources: int,
    tech_points: int | None,
    insider_points: float,
    buzz: bool,
    reaction_atr: float | None,
    direction: str,
    rumor: bool,
) -> tuple[int, list[str]]:
    notes = []
    score = 1.0
    score += catalyst_weight
    notes.append(f"Katalysator {sde(catalyst_weight)}")
    cred = (credibility - 1) / 4 * 2
    score += cred
    notes.append(f"Glaubwürdigkeit {sde(cred)}")
    if n_sources >= 2:
        bonus = 0.5 if n_sources == 2 else 1.0
        score += bonus
        notes.append(f"{n_sources} Quellen {sde(bonus)}")
    if tech_points is not None:
        t = tech_points * 0.75
        score += t
        notes.append(f"Technik {tech_points}/4 {sde(t)}")
    if insider_points and direction != "Short":
        i = insider_points * 0.5
        score += i
        notes.append(f"Insider-Käufe {sde(i)}")
    if buzz:
        score += 0.5
        notes.append("Reddit-Erwähnungen steigen +0,5")
    if reaction_atr is not None:
        signed = reaction_atr if direction != "Short" else -reaction_atr
        if signed >= 3:
            score -= 2.5
            notes.append(f"Kurs hat schon stark reagiert ({sde(reaction_atr)} ATR) −2,5")
        elif signed >= 1.5:
            score -= 1.5
            notes.append(f"Kurs hat schon reagiert ({sde(reaction_atr)} ATR) −1,5")
        elif abs(reaction_atr) < 0.5:
            score += 0.5
            notes.append("Noch keine Kursreaktion +0,5")
    if rumor:
        score = min(score - 1.5, 7.0)
        notes.append("Gerücht −1,5 (max. 7)")
    if direction == "Neutral":
        score = min(score - 1.5, 5.0)
        notes.append("Richtung unklar −1,5 (max. 5)")
    return int(max(1, min(10, round(score)))), notes


def reaction_text(pct: float, in_atr: float) -> str:
    if abs(in_atr) >= 3:
        level = "stark reagiert"
    elif abs(in_atr) >= 1.5:
        level = "bereits reagiert"
    elif abs(in_atr) >= 0.5:
        level = "leicht reagiert"
    else:
        level = "noch nicht reagiert"
    return f"{level} ({sde(pct)} % / {sde(in_atr)} ATR seit der Meldung)"


def trade_plan(tech: Technicals, direction: str, reaction_atr: float, cfg: dict) -> TradePlan | None:
    if direction not in ("Long", "Short"):
        return None
    a = tech.atr
    stop_mult = float(cfg.get("stop_atr", 1.5))
    t1_r, t2_r = float(cfg.get("ziel1_r", 1.5)), float(cfg.get("ziel2_r", 3.0))
    sign = 1 if direction == "Long" else -1
    stretched = sign * reaction_atr >= 1.5
    if stretched:
        # Nach starker Reaktion nicht hinterherlaufen, sondern auf Rücksetzer warten.
        lo, hi = sorted((tech.close - sign * 1.0 * a, tech.close - sign * 0.3 * a))
    else:
        lo, hi = sorted((tech.close - sign * 0.3 * a, tech.close + sign * 0.2 * a))
    mid = (lo + hi) / 2
    risk = stop_mult * a
    stop = mid - sign * risk
    rules = [
        f"Stop-Loss bei {de(stop)} ({de(stop_mult, 1)} × ATR {'unter' if sign > 0 else 'über'} Einstieg) – ohne Ausnahme ausführen.",
        f"Bei Ziel 1 ({de(mid + sign * t1_r * risk)}) die Hälfte schließen und Stop auf Einstand ziehen.",
        f"Rest mit Trailing-Stop 2 × ATR ({de(2 * a)}) hinter dem besten Schlusskurs oder bei Schlusskurs "
        f"{'unter' if sign > 0 else 'über'} SMA20 schließen.",
        "Zeitstopp: kein Fortschritt Richtung Ziel 1 nach 10 Handelstagen → Position schließen.",
        "Sofort neu bewerten bei gegenläufigen News, Dementi oder Insider-Verkäufen.",
    ]
    if stretched:
        rules.insert(0, "Kurs ist bereits gelaufen: nur Limit-Order im Einstiegsbereich, nicht hinterherkaufen.")
    return TradePlan(
        entry_low=lo,
        entry_high=hi,
        stop=stop,
        target1=mid + sign * t1_r * risk,
        target2=mid + sign * t2_r * risk,
        exit_rules=rules,
    )
