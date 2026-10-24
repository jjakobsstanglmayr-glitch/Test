"""Kommandozeile: ``python -m news_scanner [--demo] [--config ...]``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from news_scanner import report
from news_scanner.config import load_config, load_positions
from news_scanner.scanner import Scanner

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="news_scanner", description="News-Scanner: Nachrichten, Insider, Stimmung.")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"), help="Pfad zu config.yaml")
    ap.add_argument("--positionen", default=None, help="Pfad zu positionen.yaml")
    ap.add_argument("--ausgabe", default=None, help="Ordner für Berichte (Standard: reports/)")
    ap.add_argument("--demo", action="store_true", help="Ohne Netzwerk mit erfundenen Beispieldaten laufen")
    ap.add_argument("-v", "--verbose", action="store_true", help="Ausführliche Protokollausgabe")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    demo_dir = Path(__file__).parent / "demo"
    config_path = demo_dir / "config_demo.yaml" if args.demo and args.config == str(ROOT / "config.yaml") else args.config
    cfg = load_config(config_path)
    positions_path = args.positionen or (demo_dir / "positionen_demo.yaml" if args.demo else ROOT / "positionen.yaml")
    positions = load_positions(positions_path)
    out_dir = Path(args.ausgabe or ROOT / cfg.get("ausgabe", {}).get("ordner", "reports"))
    data_dir = ROOT / "data" / ("demo" if args.demo else "")

    if args.demo:
        from news_scanner.demo import DemoHttp, DemoMarket, DemoReddit

        http, market = DemoHttp(), DemoMarket()
        reddit_factory = lambda q: DemoReddit()  # noqa: E731
    else:
        from news_scanner.http import HttpClient
        from news_scanner.sources.market import YFinanceProvider
        from news_scanner.sources.reddit import RedditClient

        net = cfg.get("netzwerk", {})
        ua = net.get("user_agent", "NewsScanner/1.0")
        http = HttpClient(
            ua,
            rate_limits=net.get("mindestabstand_sekunden", {}),
            default_interval=float(net.get("standard_abstand_sekunden", 1.0)),
            timeout=float(net.get("timeout_sekunden", 20)),
            retries=int(net.get("wiederholungen", 3)),
            cache_dir=data_dir / "cache",
        )
        market = YFinanceProvider(cfg.get("kurse", {}).get("zeitraum", "6mo"),
                                  float(cfg.get("kurse", {}).get("pause_sekunden", 0.5)))

        def reddit_factory(q: dict) -> RedditClient:
            return RedditClient(http, q.get("client_id", ""), q.get("client_secret", ""), q.get("user_agent") or ua)

    scanner = Scanner(cfg, positions, http, market, data_dir, reddit_factory, demo=args.demo)
    result = scanner.run()
    path = report.write(result, out_dir)

    failed = [s for s in result.statuses if not s.ok]
    print(f"Bericht: {path}")
    print(f"{len(result.candidates)} News-Kandidaten, {len(result.insiders)} Insider-Signale, "
          f"{len(result.positions)} Positionen geprüft.")
    if failed:
        print(f"{len(failed)} Quelle(n) nicht erreichbar: " + ", ".join(s.name for s in failed))
    return 0
