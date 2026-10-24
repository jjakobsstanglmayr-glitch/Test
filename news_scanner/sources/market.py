"""Kurse und Chartdaten über yfinance."""

from __future__ import annotations

import logging
import time

import pandas as pd

log = logging.getLogger(__name__)


class YFinanceProvider:
    def __init__(self, period: str = "6mo", pause: float = 0.5):
        self.period = period
        self.pause = pause

    def history(self, ticker: str) -> tuple[pd.DataFrame, str]:
        import yfinance as yf

        time.sleep(self.pause)  # yfinance hat kein offizielles Limit – trotzdem schonend abfragen
        t = yf.Ticker(ticker)
        df = t.history(period=self.period, interval="1d", auto_adjust=False)
        if df is None or df.empty:
            raise ValueError(f"keine Kursdaten für {ticker}")
        currency = ""
        try:
            currency = t.fast_info.get("currency", "") or ""
        except Exception:  # noqa: BLE001
            pass
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna(), currency
