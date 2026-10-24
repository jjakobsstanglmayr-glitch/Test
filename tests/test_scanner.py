from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from news_scanner import report
from news_scanner.analysis import insider, scoring, technicals
from news_scanner.analysis.text import Story, assign_tickers, cluster, detect_catalysts, rate_credibility
from news_scanner.cli import main
from news_scanner.config import WatchItem, load_config
from news_scanner.demo import FIXTURES, DemoHttp, DemoMarket, DemoReddit, render_placeholders
from news_scanner.http import HttpClient, HttpError
from news_scanner.models import InsiderTrade, NewsItem
from news_scanner.scanner import Scanner
from news_scanner.sources import bafin, reddit, rss, sec

NOW = datetime.now(timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


def news(title, source="CNBC", hours=1, kind="news", publisher=""):
    return NewsItem(title=title, link=f"https://x.invalid/{abs(hash(title))}", source=source,
                    published=NOW - timedelta(hours=hours), publisher=publisher or source, kind=kind)


# ------------------------------------------------------------------ Indikatoren


def ohlc(closes, vol=1_000_000.0):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(end=pd.Timestamp(NOW.date()), periods=len(closes))
    return pd.DataFrame({"Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
                         "Close": closes, "Volume": np.full(len(closes), vol)}, index=idx)


def test_rsi_extremes():
    assert technicals.rsi(pd.Series(np.arange(1, 60, dtype=float))).iloc[-1] == pytest.approx(100)
    assert technicals.rsi(pd.Series(np.arange(60, 1, -1, dtype=float))).iloc[-1] == pytest.approx(0, abs=1e-6)


def test_atr_constant_range():
    df = ohlc(np.full(60, 100.0))
    assert technicals.atr(df).iloc[-1] == pytest.approx(2.0)


def test_compute_and_volume_ratio():
    df = ohlc(np.linspace(50, 80, 80))
    df.iloc[-1, df.columns.get_loc("Volume")] = 3_000_000
    t = technicals.compute(df)
    assert t.sma20 > t.sma50
    assert t.volume_ratio == pytest.approx(3.0)
    pts, _ = technicals.confirmation(t, "Long")
    assert pts >= 3


def test_compute_needs_history():
    with pytest.raises(ValueError):
        technicals.compute(ohlc(np.full(30, 10.0)))


def test_reaction_respects_market_close():
    closes = np.r_[np.full(59, 100.0), 110.0]
    df = ohlc(closes)
    t = technicals.compute(df)
    last = df.index[-1]
    before_open = datetime(last.year, last.month, last.day, 8, tzinfo=timezone.utc)
    after_close = datetime(last.year, last.month, last.day, 21, tzinfo=timezone.utc)
    assert technicals.reaction_since(df, before_open, t)[0] == pytest.approx(10.0)
    assert technicals.reaction_since(df, after_close, t)[0] == pytest.approx(0.0)


# ------------------------------------------------------------------ Handelsplan


def test_trade_plan_long_stop_is_1_5_atr():
    t = technicals.compute(ohlc(np.linspace(50, 80, 80)))
    plan = scoring.trade_plan(t, "Long", 0.0, {})
    mid = (plan.entry_low + plan.entry_high) / 2
    assert mid - plan.stop == pytest.approx(1.5 * t.atr)
    assert plan.target1 == pytest.approx(mid + 1.5 * 1.5 * t.atr)
    assert plan.target2 > plan.target1 > plan.entry_high


def test_trade_plan_short_mirrors():
    t = technicals.compute(ohlc(np.linspace(80, 50, 80)))
    plan = scoring.trade_plan(t, "Short", 0.0, {})
    assert plan.stop > plan.entry_high and plan.target2 < plan.target1 < plan.entry_low


def test_trade_plan_waits_for_pullback_after_big_move():
    t = technicals.compute(ohlc(np.linspace(50, 80, 80)))
    plan = scoring.trade_plan(t, "Long", 2.5, {})
    assert plan.entry_high < t.close
    assert "nicht hinterherkaufen" in plan.exit_rules[0]


def test_signal_strength_bounds_and_rumor_cap():
    top, _ = scoring.signal_strength(3, 5, 4, 4, 4, True, 0.0, "Long", False)
    low, _ = scoring.signal_strength(0, 1, 1, 0, 0, False, 4.0, "Neutral", True)
    rumor, _ = scoring.signal_strength(3, 5, 4, 4, 4, True, 0.0, "Long", True)
    assert top == 10 and low == 1 and rumor == 7


# ------------------------------------------------------------------ Text


WL = [WatchItem("HLXT", "Helix Therapeutics"), WatchItem("SAP.DE", "SAP", land="DE")]


def test_cluster_merges_same_story_across_sources():
    items = [
        news("FDA approves Helix Therapeutics lung cancer drug Velorin", "Google News HLXT", publisher="Reuters"),
        news("FDA approves Helix Therapeutics' lung cancer drug Velorin, shares surge", "CNBC"),
        news("Oil prices slip on demand worries", "MarketWatch"),
    ]
    assign_tickers(items, WL)
    stories = cluster(items)
    assert len(stories) == 2
    helix = next(s for s in stories if "HLXT" in s.tickers)
    assert set(helix.sources) == {"Reuters", "CNBC"}


def test_assign_tickers_word_boundary():
    items = [news("SAP hebt Prognose an"), news("Sapphire Rapids chips")]
    assign_tickers(items, WL)
    assert items[0].tickers == {"SAP.DE"} and not items[1].tickers


def test_rumor_and_credibility():
    s = Story([news("Apex Cloud reportedly in talks to be acquired, people familiar say", "Blog")])
    rate_credibility(s)
    assert s.rumor and s.credibility < 2
    official = Story([news("EQS-Adhoc: Nordwind hebt Prognose an", "EQS", kind="adhoc")])
    rate_credibility(official)
    assert not official.rumor and official.credibility == 5


@pytest.mark.parametrize("title,catalyst,direction", [
    ("Quantum Grid upgraded to Buy, price target raised", "Hochstufung", "Long"),
    ("Analyst stuft Bavaria Robotics nach Gewinnwarnung ab", "Herabstufung", "Short"),
    ("Nordwind Energie hebt Prognose an", "Prognoseänderung", "Long"),
    ("FDA approves Helix drug", "Zulassung", "Long"),
    ("Acme to acquire Widget Co in $2bn deal", "Übernahme/Fusion", None),
    ("Acme Q3 earnings miss estimates, shares fall", "Quartalszahlen", "Short"),
])
def test_catalysts(title, catalyst, direction):
    s = Story([news(title)])
    detect_catalysts(s)
    assert catalyst in s.catalysts
    if direction:
        assert s.direction == direction


# ------------------------------------------------------------------ Quellen-Parser


def test_parse_feed_google_news_publisher():
    xml = render_placeholders((FIXTURES / "google_Helix_Therapeutics.xml").read_text())
    items = rss.parse_feed(xml, "Google News HLXT")
    assert items[0].publisher == "Reuters"
    assert not items[0].title.endswith("Reuters")
    assert items[0].published < NOW


def test_parse_feed_rejects_garbage():
    with pytest.raises(ValueError):
        rss.parse_feed("<<<kein xml", "kaputt")


def test_parse_form4_purchase_by_ceo():
    xml = render_placeholders((FIXTURES / "form4_000190000226000013.xml").read_text())
    (t,) = sec.parse_form4(xml, "https://sec.invalid")
    assert (t.ticker, t.tx_type, t.role, t.is_ceo_cfo) == ("QGRD", "Kauf", "CEO", True)
    assert t.value == pytest.approx(15000 * 70.20)


def test_sec_user_agent_check():
    with pytest.raises(ValueError):
        sec.check_user_agent("NewsScanner Max Mustermann max.mustermann@example.com")
    sec.check_user_agent("Erika Muster erika@muster-mail.de")


def test_bafin_csv():
    text = render_placeholders((FIXTURES / "bafin_DE000DEMO0001.csv").read_text())
    trades = bafin.parse_csv(text, "NWE.DE", "https://bafin.invalid")
    cfo = trades[0]
    assert cfo.tx_type == "Kauf" and cfo.is_ceo_cfo and cfo.role == "CFO"
    assert cfo.value == pytest.approx(405450.0) and cfo.currency == "EUR"
    assert bafin.parse_de_number("1.234.567,89 EUR") == (1234567.89, "EUR")


def test_bafin_csv_unknown_format_raises():
    with pytest.raises(ValueError):
        bafin.parse_csv("a;b;c\n1;2;3\n", "X", "")


def test_reddit_mentions_today_vs_yesterday():
    posts = [{"title": "$QGRD to the moon", "selftext": "", "created_utc": (NOW - timedelta(hours=h)).timestamp()}
             for h in (1, 2, 3, 30)]
    posts.append({"title": "old", "selftext": "", "created_utc": (NOW - timedelta(hours=48)).timestamp()})
    counts = reddit.count_mentions(posts, [WatchItem("QGRD", "Quantum Grid", reddit_ticker="QGRD")], NOW)
    assert counts["QGRD"] == {"heute": 3, "vortag": 1}


def test_reddit_history_fills_missing_previous_day(tmp_path):
    path = tmp_path / "hist.json"
    reddit.merge_history({"A": {"heute": 4, "vortag": None}}, path, "2026-10-23")
    counts = reddit.merge_history({"A": {"heute": 9, "vortag": None}}, path, "2026-10-24")
    assert counts["A"]["vortag"] == 4 and counts["A"]["veraenderung"] == 5


# ------------------------------------------------------------------ Insider


def trade(name, role, top, value, days, ticker="X"):
    return InsiderTrade(ticker, "X Corp", name, role, top, "Kauf", "P", NOW - timedelta(days=days),
                        value / 10, 10, value, "USD", "", "SEC Form 4")


def test_insider_cluster_ceo_and_salary_ratio():
    trades = [trade("A", "CEO", True, 2_000_000, 1), trade("B", "Director", False, 50_000, 3)]
    (sig,) = insider.analyse_purchases(trades, {"gehalt_schaetzung": {"CEO": 1_000_000, "default": 300_000}})
    assert any("CEO" in f for f in sig.flags)
    assert any("Cluster" in f for f in sig.flags)
    assert any("2,0×" in f for f in sig.flags)
    assert sig.score == 4.0


def test_insider_ignores_small_and_sales():
    small = trade("A", "Director", False, 1_000, 1)
    sale = trade("B", "CEO", True, 5_000_000, 1)
    sale.tx_type = "Verkauf"
    assert insider.analyse_purchases([small, sale], {}) == []
    assert len(insider.sales([small, sale])["X"]) == 1


# ------------------------------------------------------------------ HTTP


def test_http_rate_limit_per_host(monkeypatch):
    client = HttpClient("t", rate_limits={"sec.gov": 0.2}, default_interval=0)
    sleeps = []
    monkeypatch.setattr("news_scanner.http.time.sleep", lambda s: sleeps.append(s))
    client._throttle("www.sec.gov")
    client._throttle("www.sec.gov")
    client._throttle("other.example")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 0.2


def test_http_retries_then_raises(monkeypatch):
    client = HttpClient("t", default_interval=0, retries=2)
    calls = []

    class Resp:
        status_code = 503
        headers = {}

    monkeypatch.setattr(client.session, "request", lambda *a, **k: calls.append(1) or Resp())
    monkeypatch.setattr("news_scanner.http.time.sleep", lambda s: None)
    with pytest.raises(HttpError):
        client.get("https://example.invalid")
    assert len(calls) == 3


# ------------------------------------------------------------------ Gesamtlauf


def run_demo(tmp_path, http=None):
    cfg = load_config(ROOT / "news_scanner/demo/config_demo.yaml")
    from news_scanner.config import load_positions

    positions = load_positions(ROOT / "news_scanner/demo/positionen_demo.yaml")
    return Scanner(cfg, positions, http or DemoHttp(), DemoMarket(), tmp_path, lambda q: DemoReddit(), demo=True).run()


def test_demo_end_to_end(tmp_path):
    res = run_demo(tmp_path)
    tickers = [c.ticker for c in res.candidates]
    assert {"HLXT", "QGRD", "NWE.DE", "APXC", "BVR.DE"} <= set(tickers)
    apxc = next(c for c in res.candidates if c.ticker == "APXC")
    assert apxc.rumor and apxc.score <= 7
    bvr = next(c for c in res.candidates if c.ticker == "BVR.DE")
    assert bvr.direction == "Short"
    assert {e.signal.ticker for e in res.insiders} >= {"QGRD", "CDLB", "NWE.DE"}
    bvr_pos = next(p for p in res.positions if p.position.ticker == "BVR.DE")
    assert any("Insider-Verkäufe" in w for w in bvr_pos.warnings)
    assert any("Stop" in w for w in bvr_pos.warnings)
    failed = [s.name for s in res.statuses if not s.ok]
    assert failed == ["Investing.com"]  # simulierter Ausfall wird abgefangen
    assert res.reddit["APXC"]["vortag"] is not None

    md = report.render(res)
    for section in ("## Top-News-Kandidaten", "## Auffällige Insider-Käufe", "## Offene Positionen", "Stop-Loss (1,5 × ATR)"):
        assert section in md


def test_all_sources_down_still_produces_report(tmp_path):
    class DeadHttp(DemoHttp):
        def get_text(self, url, cache_hours=0, **kwargs):
            raise HttpError("Verbindung verweigert")

    res = run_demo(tmp_path, DeadHttp())
    assert res.candidates == [] and res.insiders == []
    assert any(s.ok is False for s in res.statuses)
    assert "Keine Kandidaten" in report.render(res)


def test_cli_demo_writes_report(tmp_path, capsys):
    assert main(["--demo", "--ausgabe", str(tmp_path)]) == 0
    files = list(tmp_path.glob("demo_news-scan_*.md"))
    assert len(files) == 1 and "DEMO-BERICHT" in files[0].read_text()


def test_http_unreachable_host_fails_fast_afterwards(monkeypatch):
    import requests

    client = HttpClient("t", default_interval=0, retries=1)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(client.session, "request", boom)
    monkeypatch.setattr("news_scanner.http.time.sleep", lambda s: None)
    for _ in range(3):
        with pytest.raises(HttpError, match="Keine Verbindung zu dead.invalid"):
            client.get("https://dead.invalid/feed")
    assert len(calls) == 2  # nur der erste Aufruf versucht es (mit 1 Wiederholung)


# ------------------------------------------------------------------ Regressionen aus echten Schlagzeilen


@pytest.mark.parametrize("title", [
    "Apple Inc. $AAPL Stock Acquired by Stone Summit Wealth LLC",
    "Better Dividend Stock to Buy and Hold: Pfizer or Bristol Myers Squibb?",
    "Microsoft's Earnings Are Days Away: Here's What Investors Need to Know",
    "Siemens-Aktie: Zweiter Tag in Folge Gewinne",
    "Allianz-Aktie: So viel hätte eine Investition in Allianz von vor einem Jahr abgeworfen",
])
def test_noise_headlines_have_no_catalyst(title):
    s = Story([news(title)])
    detect_catalysts(s)
    assert s.catalysts == []


def test_exclusion_terms_prevent_wrong_ticker():
    wl = [WatchItem("SIE.DE", "Siemens", exclude=["Siemens Energy"], land="DE")]
    items = [news("Siemens Energy-Aktie: Irrsinn!"), news("Siemens hebt Prognose an")]
    assign_tickers(items, wl)
    assert not items[0].tickers and items[1].tickers == {"SIE.DE"}


def test_short_titles_with_common_words_not_merged():
    items = [news("Siemens Energy Aktie: Irrsinn!", "A"), news("Siemens Energy Aktie: Vom KI-Gewinner zum KI-Opfer?", "B")]
    assert len(cluster(items)) == 2


def test_google_news_unknown_publisher_low_tier():
    from news_scanner.analysis.text import source_tier

    assert source_tier(news("x", "Google News SAP.DE", publisher="Börse Global")) == 2
    assert source_tier(news("x", "Google News SAP.DE", publisher="Reuters")) == 5


def test_name_followed_by_foreign_ticker_is_not_ours():
    wl = [WatchItem("AAPL", "Apple")]
    items = [news("Apple Hospitality REIT (APLE) Moves to Buy"), news("Apple (AAPL) beats estimates"),
             news("Apple stock rises")]
    assign_tickers(items, wl)
    assert [bool(i.tickers) for i in items] == [False, True, True]
