"""Technische Kennzahlen: RSI, SMA 20/50, ATR, Volumenverhältnis, Kursreaktion."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from news_scanner.fmt import de
from news_scanner.models import Technicals


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute(df: pd.DataFrame, currency: str = "") -> Technicals:
    if len(df) < 55:
        raise ValueError("zu wenig Kurshistorie (mind. 55 Handelstage)")
    close = df["Close"]
    vol_avg = df["Volume"].iloc[-21:-1].mean()
    return Technicals(
        close=float(close.iloc[-1]),
        prev_close=float(close.iloc[-2]),
        change_pct=float(close.iloc[-1] / close.iloc[-2] - 1) * 100,
        rsi=float(rsi(close).iloc[-1]),
        sma20=float(close.rolling(20).mean().iloc[-1]),
        sma50=float(close.rolling(50).mean().iloc[-1]),
        atr=float(atr(df).iloc[-1]),
        volume_ratio=float(df["Volume"].iloc[-1] / vol_avg) if vol_avg else 0.0,
        high_20=float(df["High"].iloc[-21:-1].max()),
        low_20=float(df["Low"].iloc[-21:-1].min()),
        currency=currency,
    )


def reaction_since(
    df: pd.DataFrame, since: datetime | None, tech: Technicals, close_hour_utc: float = 20.0
) -> tuple[float, float]:
    """Kursänderung (in % und in ATR) vom letzten Schlusskurs vor ``since`` bis heute.

    Kam die Meldung nach Handelsschluss, zählt der Schlusskurs desselben Tages als Basis.
    """
    close = df["Close"]
    if since is not None:
        ts = pd.Timestamp(since).tz_convert("UTC")
        after_close = ts.hour + ts.minute / 60 >= close_hour_utc
        day = ts.normalize().tz_localize(None)
        idx = close.index.tz_localize(None) if close.index.tz is not None else close.index
        days = idx.normalize()
        before = close[(days <= day) if after_close else (days < day)]
        base = float(before.iloc[-1]) if len(before) else tech.prev_close
    else:
        base = tech.prev_close
    move = tech.close - base
    return (move / base * 100 if base else 0.0), (move / tech.atr if tech.atr else 0.0)


def confirmation(tech: Technicals, direction: str) -> tuple[int, list[str]]:
    """Technische Bestätigung 0–4 Punkte mit Begründung."""
    pts, notes = 0, []
    long = direction != "Short"
    if tech.volume_ratio >= 1.5:
        pts += 1
        notes.append(f"Volumen {de(tech.volume_ratio, 1)}× Ø20 – erhöhtes Interesse")
    else:
        notes.append(f"Volumen {de(tech.volume_ratio, 1)}× Ø20 – keine Volumenbestätigung")
    above20, above50 = tech.close > tech.sma20, tech.close > tech.sma50
    if long and above20 and above50 or not long and not above20 and not above50:
        pts += 1
        notes.append(f"Kurs {'über' if long else 'unter'} SMA20 ({de(tech.sma20)}) und SMA50 ({de(tech.sma50)})")
    else:
        notes.append(f"Kurs vs. SMA20 {de(tech.sma20)} / SMA50 {de(tech.sma50)}: Trend nicht bestätigt")
    if long and tech.sma20 > tech.sma50 or not long and tech.sma20 < tech.sma50:
        pts += 1
        notes.append("SMA20 " + (">" if long else "<") + " SMA50 (Trendrichtung passt)")
    if long and 45 <= tech.rsi <= 70 or not long and 30 <= tech.rsi <= 55:
        pts += 1
        notes.append(f"RSI {tech.rsi:.0f} im passenden Bereich")
    elif long and tech.rsi > 70 or not long and tech.rsi < 30:
        notes.append(f"RSI {tech.rsi:.0f} – {'überkauft' if long else 'überverkauft'}, Rücksetzer möglich")
    else:
        notes.append(f"RSI {tech.rsi:.0f} – kein Momentum in Signalrichtung")
    notes.append(f"ATR(14) {de(tech.atr)} ({de(tech.atr / tech.close * 100, 1)} % vom Kurs)")
    return pts, notes
