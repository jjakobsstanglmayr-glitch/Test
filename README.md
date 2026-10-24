# News-Scanner

Ein Python-Tool, das du **bei Bedarf** startest. Es sammelt Nachrichten, Insider-Geschäfte,
Pflichtmeldungen und Reddit-Stimmung aus **ausschließlich kostenlosen, offiziellen Quellen**
(RSS-Feeds und offizielle Schnittstellen), wertet sie aus und schreibt einen Markdown-Bericht
nach `reports/`.

> ⚠️ Keine Anlageberatung. Das Tool liefert Hinweise, keine Kauf- oder Verkaufsempfehlungen.

---

## Schnellstart

```bash
pip install -r requirements.txt

# 1. Demo ohne Internet (erfundene Firmen und Kurse) – zeigt Ablauf und Berichtsformat
python -m news_scanner --demo

# 2. Echter Lauf mit config.yaml und positionen.yaml
export SEC_USER_AGENT="Vorname Nachname deine@mail.de"        # Pflicht für SEC EDGAR
export REDDIT_CLIENT_ID="..." REDDIT_CLIENT_SECRET="..."      # optional, für Reddit
python -m news_scanner
```

Der Bericht landet in `reports/news-scan_JJJJ-MM-TT_HHMM.md`. Ein Beispiel liegt unter
[`reports/beispielbericht_demo.md`](reports/beispielbericht_demo.md).

Weitere Optionen:

| Option | Bedeutung |
|---|---|
| `--config PFAD` | andere Konfigurationsdatei |
| `--positionen PFAD` | andere Positionsdatei |
| `--ausgabe ORDNER` | Berichte woanders ablegen |
| `--demo` | Beispieldaten statt Netzwerk |
| `-v` | ausführliches Protokoll |

---

## Quellen

Alle Quellen lassen sich in `config.yaml` einzeln ein-/ausschalten (`aktiv: false`) und anpassen.

| Bereich | Quelle | Zugang |
|---|---|---|
| Nachrichten | CNBC, MarketWatch, Yahoo Finance, Handelsblatt, finanzen.net, Investing.com | öffentliche RSS-Feeds |
| Nachrichten | **Reuters** | Reuters hat seine RSS-Feeds eingestellt → Google-News-RSS mit `site:reuters.com` |
| Nachrichten | Google News **pro Watchlist-Ticker** | Google-News-RSS-Suche (US-Werte englisch, DE-Werte deutsch) |
| Insider USA | **SEC EDGAR Form 4** | `data.sec.gov/submissions` + Original-XML; zusätzlich marktweit der neueste Form-4-Atom-Feed |
| Insider DE | **BaFin Directors' Dealings** | CSV-Export der öffentlichen BaFin-Datenbank (pro ISIN) |
| Pflichtmeldungen | **SEC 8-K** | aus `data.sec.gov/submissions`, Items werden übersetzt (z. B. 2.02 = Quartalszahlen) |
| Pflichtmeldungen | **Ad-hoc (EQS/DGAP)** | EQS-News-RSS |
| Stimmung | Reddit r/stocks, r/wallstreetbets, r/mauerstrassenwetten | offizielle Reddit-API (OAuth, kostenlose Stufe) |
| Kurse | yfinance | Tageskerzen der letzten 6 Monate |

### Einrichtung der Zugänge

**SEC EDGAR** – kein Schlüssel nötig, aber die SEC verlangt laut
[Zugriffsregeln](https://www.sec.gov/os/accessing-edgar-data) einen User-Agent mit Name und
E-Mail-Adresse und maximal 10 Anfragen pro Sekunde. Trage ihn in `config.yaml`
(`quellen.sec.user_agent`) ein oder setze `SEC_USER_AGENT`. Mit dem Platzhalter aus der
Vorlage wird die SEC-Abfrage bewusst übersprungen.

**Reddit** – unter <https://www.reddit.com/prefs/apps> eine App vom Typ „script“ anlegen
(kostenlos für private, nicht-kommerzielle Nutzung). Client-ID und Secret als
`REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` setzen und in `quellen.reddit.user_agent` deinen
Reddit-Namen eintragen. Ohne Zugangsdaten läuft der Scanner einfach ohne Reddit.

**BaFin** – kein Zugang nötig. Für jeden deutschen Wert in der Watchlist muss die `isin`
eingetragen sein. Die BaFin bietet keine dokumentierte API; das Tool nutzt den CSV-Export der
Datenbank-Suche und erkennt Spalten über ihre Überschriften. Ändert die BaFin das Format,
erscheint im Quellenstatus ein Hinweis; die URL ist unter `quellen.bafin.url` anpassbar.

**EQS Ad-hoc** – der Feed-Pfad hat sich in der Vergangenheit mehrfach geändert. Falls im
Quellenstatus ein Fehler erscheint, die aktuelle RSS-Adresse von eqs-news.com in
`quellen.adhoc.feeds` eintragen.

### Abfrage-Limits und Fehlerbehandlung

- Pro Host gilt ein Mindestabstand zwischen Anfragen (`netzwerk.mindestabstand_sekunden`,
  z. B. SEC 0,15 s ≙ < 10/s, Reddit 1 s ≙ 60/min, Google News 2 s).
- Antworten mit 429/5xx werden mit wachsender Pause wiederholt, `Retry-After` wird beachtet.
- Ist ein Host nicht erreichbar, wird er für den Rest des Laufs übersprungen (kein minutenlanges Warten).
- Die SEC-Tickerliste wird 24 h zwischengespeichert (`data/cache/`).
- **Jede Quelle ist einzeln abgesichert**: fällt eine aus, läuft der Rest weiter. Am Ende des
  Berichts zeigt der Abschnitt „Quellenstatus“, was funktioniert hat und was nicht.

---

## Auswertung

1. **Ticker zuordnen** – über Google-News-Feed pro Ticker, `$TICKER`, `(NASDAQ: TICKER)` sowie
   Name und `aliases` aus der Watchlist.
2. **Zusammenführen** – gleiche Meldungen aus mehreren Quellen werden über Wortüberschneidung
   der Titel zu einer „Story“ gebündelt; jede Story listet alle Quellen.
3. **Glaubwürdigkeit (1–5)** – Pflichtmeldungen (8-K, Ad-hoc), SEC/BaFin, Reuters = 5;
   CNBC, MarketWatch, Handelsblatt = 4; Yahoo, finanzen.net, Investing.com = 3; Unbekannte = 2.
   Mehrere unabhängige Quellen erhöhen den Wert. Formulierungen wie „reportedly“,
   „people familiar“, „Insidern zufolge“, „angeblich“ führen zur Kennzeichnung **⚠️ Gerücht**
   (außer bei offiziellen Meldungen).
4. **Katalysator** – Übernahme/Fusion, Zulassung, Quartalszahlen, Prognoseänderung,
   Hoch-/Herabstufung, Management-Wechsel, Großauftrag, Rechtliches, Kapitalmaßnahme,
   Insider-Käufe (Deutsch und Englisch). Die Richtung (Long/Short) ergibt sich aus positiven
   bzw. negativen Signalwörtern.
5. **Schon reagiert?** – Kursänderung seit dem letzten Schlusskurs vor der Meldung, in % und in
   ATR (Meldungen nach Börsenschluss zählen ab dem Schlusskurs desselben Tages).
6. **Technische Bestätigung** – Volumen ggü. 20-Tage-Schnitt, Kurs über/unter SMA20 und SMA50,
   SMA20 vs. SMA50, RSI(14), ATR(14).
7. **Insider** – nur Käufe am offenen Markt (SEC-Code `P` bzw. BaFin „Kauf“). Hervorgehoben:
   Kauf durch **CEO/CFO**, **Cluster** (≥ 2 Insider innerhalb von 10 Tagen) und **großer Betrag
   im Verhältnis zum Gehalt** (≥ 50 % eines geschätzten Jahresgehalts; Schätzwerte je Rolle in
   `insider.gehalt_schaetzung`, genauere Werte je Ticker in `insider.gehalt_pro_ticker`).
8. **Reddit** – Anzahl Beiträge mit Erwähnung (Cashtag, Kürzel, Firmenname) in den letzten 24 h
   gegenüber den 24 h davor; reicht die Abfrage nicht so weit zurück, wird der Wert des
   Vortageslaufs aus `data/reddit_verlauf.json` verwendet.

### Signalstärke 1–10

| Baustein | Punkte |
|---|---|
| Grundwert | 1 |
| Katalysator (Übernahme/Zulassung 3, Zahlen/Prognose 2,5, Hoch-/Herabstufung 2, …) | 0 – 3 |
| Glaubwürdigkeit | 0 – 2 |
| Mehrere Quellen | 0 – 1 |
| Technische Bestätigung (je erfülltem Kriterium 0,75) | 0 – 3 |
| Insider-Käufe (nur Long) | 0 – 2 |
| Reddit-Erwähnungen steigen deutlich | 0,5 |
| Noch keine Kursreaktion | +0,5 |
| Kurs schon ≥ 1,5 ATR / ≥ 3 ATR gelaufen | −1,5 / −2,5 |
| Gerücht | −1,5 und höchstens 7 |
| Richtung unklar | −1,5 |

### Handelsplan je Kandidat

- **Einstiegsbereich**: Schlusskurs −0,3 bis +0,2 ATR; ist der Kurs schon ≥ 1,5 ATR gelaufen,
  wird ein Rücksetzer-Bereich (−1,0 bis −0,3 ATR) vorgeschlagen.
- **Stop-Loss**: 1,5 × ATR(14) unter (Long) bzw. über (Short) der Mitte des Einstiegsbereichs.
- **Kursziel 1 / 2**: 1,5 R bzw. 3 R (R = Abstand Einstieg–Stop). Alles in `handelsplan` einstellbar.
- **Ausstiegsregeln**: Stop konsequent, bei Ziel 1 Hälfte verkaufen und Stop auf Einstand,
  Rest per Trailing-Stop 2 × ATR bzw. Schlusskurs unter SMA20, Zeitstopp nach 10 Handelstagen,
  sofortige Neubewertung bei Gegennachrichten, Dementi oder Insider-Verkäufen.

---

## Offene Positionen

In `positionen.yaml` trägst du deine Positionen ein:

```yaml
positionen:
  - ticker: AAPL
    stueck: 20
    einstieg: 225.40
    stop: 212.00
    datum: 2026-09-15
    notiz: optional
```

Bei jedem Lauf wird pro Position geprüft und im Abschnitt „Offene Positionen“ gezeigt:
aktueller Kurs und Gewinn/Verlust, Stop erreicht oder knapp davor, Kurs unter SMA50,
**neue Meldungen** zum Wert (negative werden als Warnung markiert), **Insider-Verkäufe**
(SEC Form 4 Code `S` bzw. BaFin „Verkauf“) und ein Stop-Vorschlag mit 1,5 × ATR.
Positionen, die nicht in der Watchlist stehen, werden automatisch mit abgefragt.

---

## Projektstruktur

```
config.yaml              Quellen, Watchlist, Limits, Bewertungsparameter
positionen.yaml          offene Positionen
news_scanner/
  cli.py                 Kommandozeile
  scanner.py             Ablauf: Quellen → Auswertung → Kandidaten/Positionen
  http.py                HTTP mit Limits pro Host, Wiederholungen, Cache
  sources/               rss.py, sec.py, bafin.py, reddit.py, market.py
  analysis/              text.py (Ticker, Dubletten, Glaubwürdigkeit, Katalysatoren),
                         technicals.py, insider.py, scoring.py
  report.py              Markdown-Bericht
  demo/                  Beispieldaten (erfunden) für --demo und Tests
reports/                 erzeugte Berichte
tests/                   pytest-Tests
```

## Tests

```bash
pip install pytest
python -m pytest
```

Die Tests prüfen Indikatoren, Handelsplan, Signalstärke, Dubletten-Erkennung, Katalysatoren,
Gerüchte, die Parser für RSS/Form 4/BaFin-CSV/Reddit, Abfrage-Limits und Wiederholungen sowie
einen kompletten Lauf mit Beispieldaten – inklusive Ausfall einzelner bzw. aller Quellen.
